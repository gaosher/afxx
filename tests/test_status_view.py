"""Small public status views with complete opt-in report access."""
import copy
import json
import subprocess
import sys
import pytest
from afxx.job_view import status_view
from afxx import jobs, cli, config, common


@pytest.fixture
def report():
    return {'job': 'a'*32, 'state': 'failed', 'exit_code': 1, 'error': 'retest failed',
            'results_dir': '/results', 'log': '/job/job.log',
            'result.json': {'state': 'failed', 'exit_code': 1, 'error': 'retest failed',
               'passed': False, 'deployment': 'b'*32, 'results_dir': '/results',
               'resources': {'results_dir': '/results', 'dump': True},
               'attempts': [{'id': 'af-1', 'seed': '123', 'backend': 'af-mlir', 'status': 'error',
                    'shape_results': [{'shape_index': 1, 'correctness': 'error'}],
                    'metadata': {'graph': 'large graph'*10000},
                    'contexts': {'execution': {'diagnostics': 'large log'*10000}}},
                    {'id': 'cpu-1', 'backend': 'cpu', 'status': 'ok',
                     'shape_results': [{'shape_index': 1, 'correctness': 'not_checked'}]}],
               'evidence': {'files': [{'path': '/dump/a.mlir', 'excerpt': 'IR'*10000}], 'errors': []}},
            'environment.json': {'dependencies': {'passed': True, 'checks': [{'name': 'torch', 'passed': True, 'stdout': 'loaded'*10000}]}}}


def test_summary_is_small_and_does_not_change_source(report):
    before = copy.deepcopy(report)
    data = status_view(report)
    assert len(json.dumps(data)) < 2000
    assert data['job_id'] == 'a'*32 and 'job' not in data
    assert data['state'] == 'failed' and data['replay']['passed'] is False
    assert data['replay']['failures'] == [{'attempt_id': 'af-1', 'shape_indices': [1]}]
    assert data['replay']['attempt_count'] == 2 and data['replay']['failed_attempt_count'] == 1
    assert data['dependencies'] == {'passed': True, 'failed_checks': []}
    assert 'results_dir' not in data and 'details' not in data
    assert report == before


def test_full_and_attempt_keep_original_afx_details(report):
    full = status_view(report, full=True)
    detail = full['details']['result.json']
    assert 'state' not in detail and 'exit_code' not in detail and 'error' not in detail
    assert 'results_dir' not in detail['resources']
    assert detail['deployment_job_id'] == 'b'*32
    assert detail['attempts'] == report['result.json']['attempts']
    one = status_view(report, attempt_id='af-1', fields=list(report['result.json']['attempts'][0]))
    assert one['attempt'] == report['result.json']['attempts'][0]
    assert 'details' not in one and 'replay' not in one
    with pytest.raises(ValueError, match='not found'):
        status_view(report, attempt_id='absent')


def test_remote_returns_summary_before_transport(report, tmp_path, monkeypatch):
    job = tmp_path / ('a'*32); job.mkdir()
    for name in ('result.json', 'environment.json'):
        (job/name).write_text(json.dumps(report[name]))
    (job/'status.json').write_text(json.dumps({k:v for k,v in report.items() if not k.endswith('.json')}))
    def remote(c, argv):
        return json.loads(subprocess.check_output([sys.executable,*argv[1:]], text=True))
    monkeypatch.setattr(jobs, 'remote', remote)
    cfg = {'remote': {'state_dir': str(tmp_path)}}
    data = jobs.query_status(cfg, job.name)
    assert len(json.dumps(data)) < 2000
    assert jobs.query_status(cfg, job.name, full=True)['details']['result.json']['attempts'] == report['result.json']['attempts']
    assert jobs.query_status(cfg, job.name, attempt_id='af-1')['attempt']['id'] == 'af-1'
    with pytest.raises(common.WorkflowError) as error:
        jobs.query_status(cfg, job.name, attempt_id='missing')
    assert error.value.code == 'ATTEMPT_NOT_FOUND'
    assert jobs.query_status(cfg, 'c'*32)['state'] == 'not_found'
    assert not (tmp_path/('c'*32)).exists()
    assert json.loads((job/'result.json').read_text()) == report['result.json']


def test_task_state_is_preserved():
    assert status_view({'job': 'a'*32, 'state': 'running'})['state'] == 'running'


def test_failed_dependencies_excludes_successes():
    state = 'failed'
    value = status_view({'job':'a'*32,'state':state,'environment.json':{'dependencies':{
        'passed':False,'checks':[{'name':'ssl','passed':False,'stderr':'missing'}, {'name':'torch','passed':True}]}}})
    assert value['state'] == state and value['dependencies']['failed_checks'][0]['name']=='ssl'
    assert len(value['dependencies']['failed_checks']) == 1


def test_submission_has_one_job_id_and_flat_status(report):
    data = cli.response('replay',{'job':'a'*32,'state':'failed','status':report})['data']
    assert data['job_id']=='a'*32 and 'job' not in data and 'status' not in data
    assert 'result.json' not in data and data['replay']['passed'] is False


@pytest.mark.parametrize('args', [[], ['--attempt','af-1']])
def test_cli_detail_routing(monkeypatch, capsys, args):
    monkeypatch.setattr(sys,'argv',['afxx','status','--request-id','a'*32,*args])
    monkeypatch.setattr(config,'config',lambda _: {})
    monkeypatch.setattr(jobs,'local_preflight',lambda *a:None)
    monkeypatch.setattr(jobs,'cleanup_after_status',lambda *a:None)
    monkeypatch.setattr(jobs,'query_status',lambda c,j,**kw:dict(state='success',job_id=j,**kw))
    assert cli.cli()==0
    data=json.loads(capsys.readouterr().out)['data']
    assert data.get('full', False)==('--full' in args)
    assert data['attempt_id']==('af-1' if '--attempt' in args else None)


def test_default_attempt_is_small_and_fields_are_explicit(report):
    attempt = report['result.json']['attempts'][0]
    attempt['error_summary'] = 'error'*1000
    small = status_view(report, attempt_id='af-1')
    assert len(json.dumps(small)) < 2000
    assert 'metadata' not in small['attempt'] and 'contexts' not in small['attempt']
    assert small['attempt_truncated_fields'] == ['error_summary']
    selected = status_view(report, attempt_id='af-1', fields=['metadata.graph','shape_results'])
    assert selected['attempt']['metadata.graph'] == attempt['metadata']['graph']
    assert set(selected['attempt']) == {'metadata.graph','shape_results'}
    with pytest.raises(ValueError, match='field not found'):
        status_view(report, attempt_id='af-1', fields=['typo'])


@pytest.mark.parametrize('scenario', ['changed','timeout','terminal','attention','since'])
def test_wait_only_returns_changes_or_end(monkeypatch, scenario):
    import time
    now=[0]
    monkeypatch.setattr(time,'monotonic',lambda:now[0])
    monkeypatch.setattr(time,'sleep',lambda seconds:now.__setitem__(0,now[0]+seconds))
    initial={'job_id':'a'*32,'state':'running','phase':'prepare'}
    count=[0]
    def query(*a,**kw):
        count[0]+=1
        if scenario=='terminal':return dict(initial,state='failed')
        if scenario=='attention':return dict(initial,state='unknown')
        if scenario=='changed' and count[0]>1:return dict(initial,phase='replay')
        return initial
    monkeypatch.setattr(jobs,'query_status',query)
    result=jobs.wait_status({},'a'*32,since='0'*64 if scenario=='since' else None,timeout=4)
    assert result['event']=={'changed':'changed','timeout':'timeout','terminal':'terminal','attention':'attention','since':'changed'}[scenario]
    if scenario=='timeout':assert set(result)=={'job_id','event','cursor'}
    assert now[0]<=4


@pytest.mark.parametrize('args', [['--fields','metadata'], ['--since','abc'], ['--timeout','3'], ['--full','--wait']])
def test_invalid_status_modes(monkeypatch,capsys,args):
    monkeypatch.setattr(sys,'argv',['afxx','status','--request-id','a'*32,*args])
    monkeypatch.setattr(config,'config',lambda _: {})
    monkeypatch.setattr(jobs,'local_preflight',lambda *a:None)
    assert cli.cli()==2
    assert json.loads(capsys.readouterr().out)['error']['code']=='INVALID_ARGUMENT'
