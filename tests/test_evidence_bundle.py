"""Complete replay evidence downloads, caching and compact terminal results."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from afxx import common, jobs, evidence_bundle as bundle


@pytest.fixture(params=[False, True], ids=["flat", "grouped"])
def scene(tmp_path, monkeypatch, request):
    root=tmp_path.resolve()
    job_id='a'*32
    relative = 'repair/replay/run-1' if request.param else job_id
    job=root/'remote'/relative;job.mkdir(parents=True)
    results=root/'results'/job_id;(results/'dump').mkdir(parents=True)
    (job/'torch_compile_debug').mkdir()
    (job/'job.log').write_text('failure log')
    (job/'torch_compile_debug'/'graph.py').write_text('full graph')
    (job/'status.json').write_text(json.dumps({'state':'failed'}))
    (job/'request.json').write_text(json.dumps({'command':'replay', 'execution_id':job_id}))
    attempt={'id':'attempt1','seed_short':'Eseed','backend':'af-mlir','status':'error',
             'shape_results':[{'shape_index':0,'correctness':'error','error_stage':'compile','error_summary':'bad type'}]}
    (job/'result.json').write_text(json.dumps({'results_dir':str(results),'attempts':[attempt]}))
    for i in range(140): (results/'dump'/f'{i}.mlir').write_text('complete IR'*1000)
    (results/'dump'/'kernel.so').write_bytes(b'binary\0artifact')
    (results/'sync.json').write_text('SECRET')
    (results/'dump'/'outside').symlink_to(results/'sync.json')
    cfg={'connection':{'environment':'npu'},'local':{'state_dir':str(root/'local')},'remote':{'state_dir':str(root/'remote')},'replay':{'results_dir':str(results.parent)}}
    if request.param:
        common.write(root/'local'/'requests'/(common.digest(job_id)+'.json'), {'remote_relative':relative})
    monkeypatch.setattr(bundle,'cache_root',lambda:root/'cache')
    monkeypatch.setattr(bundle.shutil,'which',lambda _: '/bin/npursync')
    calls=[]
    def remote(c,argv):
        calls.append('manifest')
        return json.loads(subprocess.check_output([sys.executable,*argv[1:]],text=True))
    def transfer(argv,**kw):
        calls.append('transfer')
        listing=Path(next(v.split('=',1)[1] for v in argv if v.startswith('--files-from=')))
        source=Path(argv[-2].split(':',1)[1]);target=Path(argv[-1])
        for name in listing.read_bytes().split(b'\0'):
            if not name:continue
            dest=target/name.decode();dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/name.decode(),dest)
    monkeypatch.setattr(common,'run',transfer)
    return cfg,job_id,job,results,remote,calls


def test_download_all_files_and_reuse(scene):
    cfg,jid,job,results,remote,calls=scene
    path=bundle.download(cfg,jid,remote)
    value=json.loads((path/'evidence.json').read_text())
    assert len(value['files'])>128
    assert (path/'results/dump/kernel.so').read_bytes()==b'binary\0artifact'
    assert (path/'results/dump/0.mlir').read_text()==(results/'dump/0.mlir').read_text()
    assert not (path/'results/sync.json').exists() and not (path/'results/dump/outside').exists()
    assert {r['reason'] for r in value['skipped']}=={'symlink','upload_credentials'}
    before=list(calls)
    assert bundle.download(cfg,jid,remote)==path and calls==before
    result=bundle.terminal_result(path,{'job_id':jid,'state':'failed'})
    assert set(result)=={'job_id','state','evidence_path','failures'}
    assert result['failures'][0]['stage']=='compile' and result['failures'][0]['error']=='bad type'
    assert result['failures'][0]['seed']=='Eseed'


def test_cache_repair_preserves_old_local_files(scene):
    cfg,jid,job,results,remote,calls=scene
    path=bundle.download(cfg,jid,remote)
    (path/'results/dump/0.mlir').write_text('local edit')
    bundle.download(cfg,jid,remote)
    assert len(calls)==6
    old=next(path.parent.glob(path.name+'.previous-*'))
    assert (old/'results/dump/0.mlir').read_text()=='local edit'
    assert (path/'results/dump/0.mlir').read_text()!='local edit'


def test_partial_download_is_not_cached(scene,monkeypatch):
    cfg,jid,job,results,remote,calls=scene
    monkeypatch.setattr(common,'run',lambda *a,**kw: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(common.WorkflowError) as error:bundle.download(cfg,jid,remote)
    assert error.value.code=='EVIDENCE_DOWNLOAD_FAILED'
    path=Path(error.value.data['partial_path'])
    assert path.exists() and not (path/'evidence.json').exists()


def test_active_replay_is_not_downloaded(scene):
    cfg,jid,job,results,remote,calls=scene
    data={'state':'running','replay':{}}
    assert jobs.localize_replay(cfg,jid,data)==data and calls==[]
    (job/'status.json').write_text(json.dumps({'state':'running'}))
    with pytest.raises(ValueError,match='terminal'):bundle.manifest(job,results)


def test_status_and_wait_localize_terminal(scene,monkeypatch):
    cfg,jid,job,results,remote,calls=scene
    monkeypatch.setattr(jobs,'remote',remote)
    status={'job_id':jid,'state':'failed','replay':{}}
    monkeypatch.setattr(jobs,'query_status',lambda *a,**kw:status)
    first=jobs.localize_replay(cfg,jid,status)
    before=list(calls)
    second=jobs.wait_status(cfg,jid,timeout=1)
    assert first==second and calls==before
