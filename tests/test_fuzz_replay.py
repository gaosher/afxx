import json
import sqlite3
import subprocess
import sys
import types

import pytest
from afxx import common
from afxx.afx import fuzz_replay as m


def attempt(backend='af-mlir'):
    return dict(id=backend, seed='123', backend=backend, status='ok',
                shape_results=[dict(shape_index=0, correctness='not_checked' if backend=='cpu' else 'pass')],
                contexts={'environment': {'autofuse_binary': 'actual'}})


@pytest.mark.parametrize('failure', [None, 'exit', 'missing', 'timeout', 'shape', 'upload'])
def test_exact_attempts_no_forced_database(cfg, tmp_path, monkeypatch, failure):
    monkeypatch.setenv('AUTOFUSEX_HOME', str(tmp_path))
    job=tmp_path/'job'; job.mkdir()
    paths=types.ModuleType('autofusex.client.paths')
    paths.data_home=lambda:tmp_path
    paths.results_database=lambda home:home/'shared.sqlite'
    monkeypatch.setitem(sys.modules, paths.__name__, paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client,'selected_client',lambda:None)
    token=tmp_path/'token';token.write_text('test-only')
    monkeypatch.setattr(m.config,'platform_settings',lambda c:{'client_token_file':str(token)})
    def upload(c, rows):
        if failure == 'upload':
            raise common.WorkflowError('REPLAY_UPLOAD_FAILED', 'server unavailable')
        return {'receipt': {'uploaded': len(rows)}}
    monkeypatch.setattr(m, 'upload_results', upload)
    cfg['replay'].update(devices='auto',results_dir=str(tmp_path/'results'),dump=True)
    common.write(job/'config.yaml',cfg)
    common.write(job/'request.json',{'seeds':['short-seed']})
    monkeypatch.setattr(common,'capture',lambda *a:'[["123",1]]')
    attempts=[attempt('cpu'),attempt()]
    if failure=='missing':attempts.pop()
    if failure=='shape':attempts[-1]['shape_results']=[]
    def invoke(argv,timeout,log):
        assert '--database' not in argv and '--data-dir' not in argv and '--device' not in argv
        assert '--dump' in argv and '--save' in argv
        if failure=='timeout':raise subprocess.TimeoutExpired(argv,timeout)
        return (1 if failure=='exit' else 0), [{'attempts':[{'attempt_id':a['id']} for a in attempts]}]
    monkeypatch.setattr(m,'invoke',invoke)
    def read(database,identity):
        assert database==tmp_path/'shared.sqlite'
        return next(a for a in attempts if a['id']==identity)
    monkeypatch.setattr(m,'read_attempt',read)
    if failure:
        with pytest.raises(common.WorkflowError):m.execute(job)
    else:
        m.execute(job)
        report=common.read(job/'result.json')
        assert report['passed'] and report['upload']['state']=='synced'
        assert len(report['execution_environments'])==2
    report = common.read(job/'result.json')
    if failure == 'upload':
        assert report['passed'] and report['batch']['complete']
        assert report['upload']['state'] == 'failed'
    if failure in ('missing', 'shape'):
        assert not report['batch']['complete']
        assert report['references'][0]['state'] == 'result_unknown'
    assert not (tmp_path/'results/job/results.sqlite').exists()
    if failure=='timeout':
        assert common.read(job/'result.json')['references'][0]['state']=='submission_unknown'


def test_queue_exact_tasks_and_timeout(tmp_path):
    queue=tmp_path/'runs/run/tasks.sqlite';queue.parent.mkdir(parents=True)
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE tasks(id,state,attempt_id,seed,execution,reason)')
        db.executemany('INSERT INTO tasks VALUES(?,?,?,?,?,?)', [('own','completed','a','123','{}',''),('other','running',None,'456','{}','')])
    checkpoints=[]
    result=m.resolve_receipt({'queue':str(queue),'task_ids':['own']},tmp_path,tmp_path/'fallback',0,checkpoints.append)
    assert result==(queue.parent/'results.sqlite',['a'])
    assert len(checkpoints[0])==1
    for ids in ([],['own','own'],['missing']):
        with pytest.raises(common.WorkflowError):m.task_rows(queue,ids,tmp_path)
    with pytest.raises(common.WorkflowError,match='still running'):
        m.resolve_receipt({'queue':str(queue),'task_ids':['other']},tmp_path,tmp_path/'fallback',0,checkpoints.append)


def test_noisy_receipt_and_exact_public_result(monkeypatch,tmp_path):
    assert m.objects('CANN log\n{\n"task_ids": ["a"]\n}\nmore log\n')==[{'task_ids':['a']}]
    def run(argv,**kwargs):
        assert argv[-3:]==[str(tmp_path/'db'),'--attempt','a']
        return subprocess.CompletedProcess(argv,0,'noise\n{"id":"a"}\n','')
    monkeypatch.setattr(m.subprocess,'run',run)
    assert m.read_attempt(tmp_path/'db','a')=={'id':'a'}


def test_timeout_stops_cli_group(monkeypatch,tmp_path):
    signals=[]
    class Process:
        pid=123
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def wait(self,timeout=None):
            if timeout!=10:raise subprocess.TimeoutExpired('afx',timeout)
            return -15
    monkeypatch.setattr(m.subprocess,'Popen',lambda argv,**kw:Process())
    monkeypatch.setattr(m.os,'killpg',lambda *a:signals.append(a))
    with pytest.raises(subprocess.TimeoutExpired):m.invoke(['afx'],1,tmp_path/'log')
    assert signals==[(123,m.signal.SIGTERM)]


def test_failed_results_are_uploaded(cfg,tmp_path,monkeypatch):
    monkeypatch.setenv('AUTOFUSEX_HOME',str(tmp_path))
    paths=types.ModuleType('autofusex.client.paths')
    paths.data_home=lambda:tmp_path
    paths.results_database=lambda home:home/'db'
    monkeypatch.setitem(sys.modules,paths.__name__,paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client,'selected_client',lambda:None)
    token=tmp_path/'token';token.write_text('test-only')
    monkeypatch.setattr(m.config,'platform_settings',lambda c:{'client_token_file':str(token)})
    cfg['replay']['results_dir']=str(tmp_path/'results')
    job=tmp_path/'job';job.mkdir()
    common.write(job/'config.yaml',cfg);common.write(job/'request.json',{'seeds':['seed']})
    monkeypatch.setattr(common,'capture',lambda *a:'[["123",1]]')
    monkeypatch.setattr(m,'invoke',lambda *a:(0,[{'attempts':[{'attempt_id':'cpu'},{'attempt_id':'af-mlir'}]}]))
    def read(database,identity):
        value=attempt(identity)
        if identity!='cpu':value['shape_results'][0]['correctness']='fail'
        return value
    monkeypatch.setattr(m,'read_attempt',read)
    uploads=[]
    def upload(c,attempts):
        uploads.append(attempts)
        return {'receipt':{'uploaded':2}}
    monkeypatch.setattr(m,'upload_results',upload)
    with pytest.raises(common.WorkflowError,match='fuzz replay failed'):m.execute(job)
    assert len(uploads) == 1
    assert common.read(job/'result.json')['upload']['enabled'] is True


def test_disabled_session_upload_is_rejected_before_submission(cfg,tmp_path,monkeypatch):
    monkeypatch.setenv('AUTOFUSEX_HOME',str(tmp_path))
    paths=types.ModuleType('autofusex.client.paths')
    paths.data_home=lambda:tmp_path
    paths.results_database=lambda home:home/'db'
    monkeypatch.setitem(sys.modules,paths.__name__,paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client,'selected_client',lambda:{'home':str(tmp_path)})
    def public(args):
        if args[0]=='daemon':return {'runs':[{'id':'r','state':'running'}]}
        return {'spec':{'spec':{'results':{'upload':False}}}}
    monkeypatch.setattr(m,'public',public)
    monkeypatch.setattr(m,'invoke',lambda *a:pytest.fail('must not submit'))
    job=tmp_path/'job';job.mkdir()
    common.write(job/'config.yaml',cfg);common.write(job/'request.json',{'seeds':['s']})
    with pytest.raises(common.WorkflowError,match='active AFX session'):m.execute(job)


def test_dump_observes_reused_worker_and_ignores_symlink(tmp_path):
    worker=tmp_path/'debug/autofuse_compile/worker-old';worker.mkdir(parents=True)
    artifact=worker/'kernel.cpp';artifact.write_text('old')
    before=m.dump_directories(tmp_path)
    artifact.write_text('new compilation output')
    import os
    os.utime(artifact, ns=(1, before[str(worker)][1]+1000000))
    assert m.dump_directories(tmp_path)[str(worker)]!=before[str(worker)]
    link=tmp_path/'debug/autofuse_compile/worker-link';link.symlink_to(worker)
    assert str(link) not in m.dump_directories(tmp_path)


@pytest.mark.parametrize('modes', [('queue',), ('queue','standalone')])
def test_client_owned_upload_never_resynced(cfg,tmp_path,monkeypatch,modes):
    monkeypatch.setenv('AUTOFUSEX_HOME',str(tmp_path))
    paths=types.ModuleType('autofusex.client.paths')
    paths.data_home=lambda:tmp_path
    paths.results_database=lambda home:home/'db'
    monkeypatch.setitem(sys.modules,paths.__name__,paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client,'selected_client',lambda:{'home':str(tmp_path)})
    def public(args):
        if args[0]=='daemon':return {'runs':[{'id':'r','state':'running'}]}
        return {'spec':{'spec':{'results':{'upload':True}}}}
    monkeypatch.setattr(m,'public',public)
    monkeypatch.setattr(m.config,'platform_settings',lambda c:pytest.fail('client owns credentials'))
    cfg['replay']['results_dir']=str(tmp_path/'results')
    job=tmp_path/'job';job.mkdir()
    common.write(job/'config.yaml',cfg);common.write(job/'request.json',{'seeds':[str(i) for i in range(len(modes))]})
    monkeypatch.setattr(common,'capture',lambda *a:json.dumps([[str(i+123),1] for i in range(len(modes))]))
    def invoke(argv,*args):
        i=int(argv[5]);receipt={'task_ids':['task'],'queue':str(tmp_path/'tasks.sqlite')} if modes[i]=='queue' else {'attempts':[]}
        return 0,[dict(receipt,index=i)]
    monkeypatch.setattr(m,'invoke',invoke)
    monkeypatch.setattr(m,'resolve_receipt',lambda receipt,*a:(tmp_path/'db',[str(receipt['index'])+'cpu',str(receipt['index'])+'af-mlir']))
    def read(db,identity):
        value=attempt(identity[1:]);value.update(id=identity,seed=str(int(identity[0])+123));return value
    monkeypatch.setattr(m,'read_attempt',read)
    uploaded=[]
    monkeypatch.setattr(m,'upload_results',lambda c,values:uploaded.extend(values) or {'receipt':{'uploaded':len(values)}})
    m.execute(job)
    report=common.read(job/'result.json')
    assert report['upload']['state']=='delegated'
    assert report['upload']['client_attempt_ids']==['0cpu','0af-mlir']
    assert [v['id'] for v in uploaded]==([] if len(modes)==1 else ['1cpu','1af-mlir'])


@pytest.mark.parametrize('state,reason,skip', [
    ('cancelled', 'no_autofuse_coverage', True),
    ('cancelled', 'user_cancelled', False),
    ('interrupted', 'no_autofuse_coverage', False),
    ('failed', '', False),
])
def test_only_explicit_coverage_cancel_is_skip(tmp_path, monkeypatch, state, reason, skip):
    rows = [dict(id='t', state=state, reason=reason, attempt_id='a')]
    monkeypatch.setattr(m, 'task_rows', lambda *a: rows)
    args = ({'queue': str(tmp_path/'tasks.sqlite'), 'task_ids': ['t']}, tmp_path, tmp_path/'db', 0, lambda rows: None)
    if skip:
        assert m.resolve_receipt(*args) == (tmp_path/'db', [])
    else:
        with pytest.raises(common.WorkflowError) as error:
            m.resolve_receipt(*args)
        assert error.value.code == 'AFX_REPLAY_FAILED'


@pytest.mark.parametrize('all_skip', [False, True])
def test_coverage_skip_continues_batch(cfg, tmp_path, monkeypatch, all_skip):
    paths = types.ModuleType('autofusex.client.paths')
    paths.data_home = lambda: tmp_path
    paths.results_database = lambda home: home/'db'
    monkeypatch.setitem(sys.modules, paths.__name__, paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client, 'selected_client', lambda: {'home': str(tmp_path)})
    monkeypatch.setattr(m, 'public', lambda args: {'runs': [{'id': 'r', 'state': 'running'}]} if args[0]=='daemon' else {'spec': {'results': {'upload': True}}})
    monkeypatch.setattr(m, 'upload_results', lambda *a: pytest.fail('client owns upload'))
    cfg['replay'].update(results_dir=str(tmp_path/'results'), dump=False)
    job = tmp_path/'job'; job.mkdir()
    common.write(job/'config.yaml', cfg)
    common.write(job/'request.json', {'seeds': ['first', 'second']})
    monkeypatch.setattr(common, 'capture', lambda *a: '[["122",1],["123",1]]')
    submitted = []
    def invoke(argv, *args):
        submitted.append(argv[5])
        return 1, [{'queue': str(tmp_path/'tasks.sqlite'), 'task_ids': [argv[5]]}]
    monkeypatch.setattr(m, 'invoke', invoke)
    def rows(queue, ids, home):
        skip = ids == ['first'] or all_skip
        return [dict(id=backend, state='cancelled' if skip else 'completed',
                     reason='no_autofuse_coverage' if skip else '', attempt_id=backend)
                for backend in ('cpu', 'af-mlir')]
    monkeypatch.setattr(m, 'task_rows', rows)
    monkeypatch.setattr(m, 'read_attempt', lambda db, identity: attempt(identity))
    with pytest.raises(common.WorkflowError) as error:
        m.execute(job)
    assert error.value.code == 'REPLAY_FAILED'
    assert submitted == ['first', 'second']
    report = common.read(job/'result.json')
    assert not report['passed']
    assert report['counts'] == {'pass': 0 if all_skip else 1, 'fail': 0, 'skip': 2 if all_skip else 1}
    assert report['references'][0]['reason'] == 'no_autofuse_coverage'
    assert report['errors'] == []
    assert report['upload']['state'] == 'delegated'


@pytest.mark.parametrize('failure,expected,continues', [('task','fail',True), ('wait','pending',True), ('read','result_unknown',True), ('protocol','blocked',False), ('submit','submission_unknown',False), ('receipt_timeout','pending',True)])
def test_batch_exception_boundaries(cfg, tmp_path, monkeypatch, failure, expected, continues):
    paths = types.ModuleType('autofusex.client.paths')
    paths.data_home = lambda: tmp_path
    paths.results_database = lambda home: home/'db'
    monkeypatch.setitem(sys.modules, paths.__name__, paths)
    from afxx.afx import managed_client
    monkeypatch.setattr(managed_client, 'selected_client', lambda: {'home': str(tmp_path)})
    monkeypatch.setattr(m, 'public', lambda args: {'runs': [{'id': 'r', 'state': 'running'}]} if args[0]=='daemon' else {'spec': {'results': {'upload': True}}})
    monkeypatch.setattr(m, 'upload_results', lambda *a: pytest.fail('client owns upload'))
    cfg['replay'].update(results_dir=str(tmp_path/'results'), dump=False)
    job = tmp_path/'job'; job.mkdir()
    common.write(job/'config.yaml', cfg)
    common.write(job/'request.json', {'seeds': ['first', 'second']})
    monkeypatch.setattr(common, 'capture', lambda *a: '[["122",1],["123",1]]')
    submitted = []
    def invoke(argv, timeout, log):
        submitted.append(argv[5])
        receipt = {'queue': str(tmp_path/'tasks.sqlite'), 'task_ids': [argv[5]]}
        if argv[5] == 'first' and failure in ('submit', 'receipt_timeout'):
            if failure == 'receipt_timeout':
                log.write_text(json.dumps(receipt))
            raise subprocess.TimeoutExpired(argv, timeout)
        return 0, [receipt]
    monkeypatch.setattr(m, 'invoke', invoke)
    def resolve(receipt, *args):
        first = receipt['task_ids'] == ['first']
        if first and failure in ('task', 'wait', 'protocol', 'receipt_timeout'):
            code = {'task': 'AFX_REPLAY_FAILED', 'wait': 'REPLAY_WAIT_TIMEOUT',
                    'protocol': 'AFX_PROTOCOL_ERROR', 'receipt_timeout': 'REPLAY_WAIT_TIMEOUT'}[failure]
            raise common.WorkflowError(code, failure)
        return tmp_path/'db', ['first'] if first else ['cpu', 'af-mlir']
    monkeypatch.setattr(m, 'resolve_receipt', resolve)
    def read(db, identity):
        if identity == 'first':
            raise common.WorkflowError('AFX_COMMAND_FAILED', 'result unavailable')
        return attempt(identity)
    monkeypatch.setattr(m, 'read_attempt', read)
    with pytest.raises(common.WorkflowError):
        m.execute(job)
    report = common.read(job/'result.json')
    assert submitted == (['first', 'second'] if continues else ['first'])
    assert report['references'][0]['state'] == expected
    assert report['batch']['complete'] == (failure == 'task')
    assert report['batch']['unsubmitted'] == ([] if continues else ['second'])
    assert report['counts'] == {'pass': int(continues), 'fail': int(failure == 'task'), 'skip': 0}
    assert not report['passed']
