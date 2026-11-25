from concurrent.futures import ThreadPoolExecutor
import sys

import pytest
from afxx import cli, common, jobs, replay_runs as runs


def test_concurrent_allocation_and_frozen_context(cfg, monkeypatch):
    monkeypatch.setattr(jobs, 'submit', lambda *a: {'state': 'queued'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runs.submit(cfg, {'seeds': ['ABC']}, 'repair'), range(8)))
    assert sorted(item['run'] for item in results) == list(range(1, 9))
    assert len({row['execution'] for row in runs._load(cfg, 'repair')['runs']}) == 8
    cfg['connection']['environment'] = 'different'
    with pytest.raises(common.WorkflowError, match='state_dir'):
        runs.list_runs(cfg, 'repair')


def test_submission_failure_remains_queryable(cfg, monkeypatch):
    def fail(*args):
        raise common.WorkflowError('REMOTE_UNAVAILABLE', 'connection lost')
    monkeypatch.setattr(jobs, 'submit', fail)
    with pytest.raises(common.WorkflowError) as error:
        runs.submit(cfg, {'seeds': ['ABC']}, 'repair')
    assert error.value.data['run'] == 1
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {'state': 'running', 'phase': 'replay'})
    view = runs.show(cfg, 'repair')
    assert view['state'] == 'running'
    assert view['submission_error']['code'] == 'REMOTE_UNAVAILABLE'
    assert 'execution' not in view and 'job_id' not in view


def test_dry_run_and_missing_history(cfg, monkeypatch):
    monkeypatch.setattr(jobs, 'submit', lambda *a: {'state': 'prepared'})
    monkeypatch.setattr(jobs, 'query_job', lambda *a: pytest.fail('dry run has no remote job'))
    runs.submit(cfg, {'seeds': ['ABC']}, 'repair', True)
    assert runs.show(cfg, 'repair')['run'] == 1
    assert runs.list_runs(cfg, 'repair')['runs'][0]['state'] == 'prepared'
    with pytest.raises(common.WorkflowError):
        runs.show(cfg, 'repair', 0)
    with pytest.raises(common.WorkflowError):
        runs.show(cfg, 'missing')


def test_compare_matches_full_seed_backend_shape(monkeypatch):
    def attempt(seed, backend, correctness, index=0):
        return dict(seed=seed, backend=backend, status='ok', shape_results=[dict(shape_index=index, correctness=correctness)])
    reports = [
        [attempt('A', 'cpu', 'not_checked'), attempt('A', 'af-mlir', 'fail'), attempt('B', 'af-mlir', 'fail')],
        [attempt('A', 'cpu', 'not_checked'), attempt('A', 'af-mlir', 'pass'), attempt('C', 'af-mlir', 'pass')],
    ]
    monkeypatch.setattr(runs, 'show', lambda c, r, n, **kw: {'state': 'success', 'result': {'attempts': reports[n-1]}})
    result = runs.compare({}, 'repair', 1, 2)
    states = {(r['seed'], r['backend']): r['comparison'] for r in result['comparisons']}
    assert states == {('A', 'cpu'): 'unchanged', ('A', 'af-mlir'): 'changed', ('B', 'af-mlir'): 'missing', ('C', 'af-mlir'): 'added'}
    reports[1].append(reports[1][1])
    assert any(r['comparison'] == 'unmatched' for r in runs.compare({}, 'repair', 1, 2)['comparisons'])
    monkeypatch.setattr(runs, 'show', lambda *a, **kw: {'state': 'running'})
    with pytest.raises(common.WorkflowError, match='已结束'):
        runs.compare({}, 'repair', 1, 2)


@pytest.mark.parametrize('action,options', [('list', []), ('show', ['--run', '2']), ('compare', ['--runs', '1', '2'])])
def test_history_cli_routes(cfg, monkeypatch, action, options):
    monkeypatch.setattr(sys, 'argv', ['afxx', 'replay', action, '--request-id', 'repair', *options])
    monkeypatch.setattr(cli.workflow_config, 'config', lambda *a: cfg)
    monkeypatch.setattr(jobs, 'local_preflight', lambda *a: None)
    monkeypatch.setattr(runs, {'list': 'list_runs'}.get(action, action), lambda c, request, *a: {'request_id': request, 'args': a})
    assert cli.main()['request_id'] == 'repair'


def test_terminal_show_downloads_evidence_and_preserves_runtime(cfg, monkeypatch):
    monkeypatch.setattr(jobs, 'submit', lambda *a: {'state': 'queued'})
    runs.submit(cfg, {'seeds': ['ABC']}, 'repair')
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {
        'state': 'failed', 'result.json': {'passed': False, 'runtime': {'pyautofuse': {'sha256': 'actual'}}},
        'environment.json': {'actual': True}})
    downloads = []
    monkeypatch.setattr(jobs, 'localize_replay', lambda *a: downloads.append(a) or {'evidence_path': '/tmp/evidence'})
    view = runs.show(cfg, 'repair')
    assert view['result']['runtime']['pyautofuse']['sha256'] == 'actual'
    assert view['environment'] == {'actual': True}
    assert view['evidence_path'] == '/tmp/evidence'
    runs.compare(cfg, 'repair', 1, 1)
    assert len(downloads) == 1  # comparison never fetches dump files


def test_status_public_id_and_removed_alias(monkeypatch):
    view = cli.response('status', {'job_id': 'repair', 'state': 'running'})
    assert view['data'] == {'request_id': 'repair', 'state': 'running'}
    monkeypatch.setattr(sys, 'argv', ['afxx', 'status', '--job-id', 'repair'])
    with pytest.raises(common.WorkflowError):
        cli.parse_arguments()


def test_history_rejects_execution_options(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['afxx', 'replay', '--dump', 'show', '--request-id', 'repair'])
    with pytest.raises(common.WorkflowError, match='不接受执行参数'):
        cli.parse_arguments()


def test_missing_seed_rejected_before_loading_config(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['afxx', 'replay', '--request-id', 'repair'])
    monkeypatch.setattr(cli.workflow_config, 'config', lambda *a: pytest.fail('must reject missing seed before config'))
    with pytest.raises(common.WorkflowError, match='--seed'):
        cli.main()


def test_replay_view_distinguishes_submission_and_execution(cfg, monkeypatch):
    monkeypatch.setattr(jobs, 'submit', lambda *a: {'state': 'queued'})
    runs.submit(cfg, {'seeds': ['ABC']}, 'repair')
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {
        'state': 'success', 'result.json': {'execution_mode': 'fuzz_replay', 'passed': True,
            'execution_environments': {'attempt': {'autofuse_binary': 'actual'}}},
        'environment.json': {'af': 'submitter'}})
    result = runs.show(cfg, 'repair', download=False)
    assert result['environment'] == {'attempt': {'autofuse_binary': 'actual'}}
    assert result['submission_environment'] == {'af': 'submitter'}


@pytest.mark.parametrize('action', ['list', 'read'])
def test_evidence_selects_run_and_pins_latest(cfg,monkeypatch,action):
    monkeypatch.setattr(jobs,'submit',lambda *a:{'state':'queued'})
    runs.submit(cfg,{'seeds':['A']},'repair')
    runs.submit(cfg,{'seeds':['B']},'repair')
    entries=runs._load(cfg,'repair')['runs']
    calls=[]
    def query(c,execution,act,**options):
        calls.append((execution,act,options))
        return {'files':[], 'next_offset':3}
    monkeypatch.setattr(jobs,'query_evidence',query)
    monkeypatch.setattr(jobs,'localize_replay',lambda *a:pytest.fail('must not download'))
    latest=runs.evidence(cfg,'repair',None,action,offset=0)
    assert latest['request_id']=='repair' and latest['run']==2
    runs.submit(cfg,{'seeds':['C']},'repair')
    runs.evidence(cfg,'repair',latest['run'],action,offset=3)
    runs.evidence(cfg,'repair',1,action,path='/remote/kernel.cpp',sha256='a'*64)
    assert [row[0] for row in calls]==[entries[1]['execution'],entries[1]['execution'],entries[0]['execution']]
    assert calls[-1][2]['sha256']=='a'*64


@pytest.mark.parametrize('problem', ['missing','run','dry','conflict'])
def test_evidence_rejects_invalid_group_before_remote(cfg,monkeypatch,problem):
    monkeypatch.setattr(jobs,'submit',lambda *a:{'state':'prepared'})
    runs.submit(cfg,{'seeds':['A']},'repair',problem=='dry')
    monkeypatch.setattr(jobs,'query_evidence',lambda *a,**k:pytest.fail('must not query remote'))
    if problem=='conflict':cfg['connection']['environment']='other'
    with pytest.raises(common.WorkflowError):
        runs.evidence(cfg,'missing' if problem=='missing' else 'repair',0 if problem=='run' else None,'list')
