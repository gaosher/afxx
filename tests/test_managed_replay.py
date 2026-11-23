import json
import sqlite3
import subprocess
import sys
import types

import pytest
from afxx import common
from afxx.afx import managed_replay as m
from afxx.afx import replay


def row(identity='t', state='completed', backend='af-mlir'):
    return dict(id=identity, state=state, attempt_id='a-' + identity, seed='123',
                execution=json.dumps(dict(backend=backend, static_only=False)), reason='')


def attempt(task):
    backend = json.loads(task['execution'])['backend']
    return dict(id=task['attempt_id'], seed=task['seed'], backend=backend, status='ok',
                shape_results=[dict(shape_index=0, correctness='not_checked' if backend == 'cpu' else 'pass')],
                contexts={'environment': {'autofuse_binary': 'executed-binary'}})


def test_shared_session_tracks_only_receipt_tasks():
    queries, checkpoints, sleeps = [], [], []
    task = row()
    def query(args):
        queries.append(args)
        if args[:2] == ['task', 'inspect']:
            return dict(state='running', spec={'environment': 'actual'})
        assert args == ['task', 'results', 'session', '--attempt', 'a-t']
        return {'attempt': attempt(task)}
    def read(identity, selected):
        assert identity == 'session' and selected == ['t']
        return [dict(task, state='completed' if sleeps else 'running')]
    result, tasks, specs = m.track(dict(run_id='session', task_ids=['t']), query, read,
                                  lambda *a: checkpoints.append(a), lambda n: sleeps.append(n))
    assert [a['id'] for a in result] == ['a-t']
    assert len(checkpoints) == 2 and sleeps == [2]
    assert specs == {'session': {'environment': 'actual'}}
    assert not any('replay' in q for q in queries)


def test_dedicated_operations_wait_then_collect_each_run():
    sleeps = []
    def query(args):
        if args[0] == 'operation':
            return {'state': 'queued'} if not sleeps else {'state': 'succeeded', 'result': {'run_id': args[2] + '-run'}}
        if args[1] == 'inspect':
            return {'state': 'completed', 'spec': {}}
        return {'attempt': attempt(row(args[2]))}
    def read(identity, ids):
        assert ids is None
        return [row(identity)]
    results, tasks, _ = m.track({'operation_ids': ['op1', 'op2']}, query, read, lambda *a: None, lambda n: sleeps.append(n))
    assert len(results) == 2 and sleeps == [2]


@pytest.mark.parametrize('state', ['failed', 'cancelled', 'needs_attention'])
def test_failed_operation_is_not_success(state):
    with pytest.raises(common.WorkflowError, match='operation failed'):
        m.track({'operation_ids': ['op']}, lambda args: {'state': state, 'result': {'code': 'failure'}},
                lambda *a: pytest.fail('no run yet'), lambda *a: None)


@pytest.mark.parametrize('rows,state', [([], 'completed'), ([row(state='cancelled')], 'running'), ([row(state='running')], 'failed')])
def test_unfinished_or_cancelled_tasks_fail(rows, state):
    with pytest.raises(common.WorkflowError):
        m.track({'run_id': 'session', 'task_ids': ['t']}, lambda args: {'state': state},
                lambda *a: rows, lambda *a: None, lambda *a: pytest.fail('must not wait'))


def test_read_only_ledger_selects_exact_ids(tmp_path):
    directory = tmp_path / 'runs/session'
    directory.mkdir(parents=True)
    path = directory / 'tasks.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('create table tasks(id,seed,state,attempt_id,execution,reason)')
        for name in ['ours', 'unrelated']:
            r = row(name)
            db.execute('insert into tasks values(?,?,?,?,?,?)', tuple(r[k] for k in ['id','seed','state','attempt_id','execution','reason']))
    before = path.read_bytes()
    assert [r['id'] for r in m.task_rows(tmp_path, 'session', ['ours'])] == ['ours']
    assert path.read_bytes() == before
    assert m.task_rows(tmp_path, 'missing', ['t']) == []
    assert not (tmp_path / 'runs/missing').exists()
    with pytest.raises(common.WorkflowError):
        m.task_rows(tmp_path, '../outside', [])


@pytest.mark.parametrize('payload,returncode,error', [
    ('noise', 0, 'AFX_PROTOCOL_ERROR'),
    (json.dumps({'ok': False, 'error': {'code': 'LookupError', 'message': 'upstream rejection'}}), 2, 'LookupError'),
    (json.dumps({'ok': True, 'data': {'state': 'failed'}}), 3, None),
])
def test_json_protocol_and_upstream_errors(monkeypatch, payload, returncode, error):
    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, returncode, payload))
    if error:
        with pytest.raises(common.WorkflowError) as caught:
            m.invoke(['afx', 'replay', 'seed'])
        assert caught.value.code == error
    else:
        assert m.invoke(['afx', 'operation', 'inspect', 'op']) == {'state': 'failed'}
    assert len(calls) == 1  # never resubmit after a missing/invalid response


def test_execute_uses_existing_home_and_records_actual_environment(cfg, tmp_path, monkeypatch):
    job, home = tmp_path / 'job', tmp_path / 'existing-client'
    job.mkdir(); home.mkdir()
    (home / 'client.json').write_text('{}')
    common.write(job / 'config.yaml', cfg)
    common.write(job / 'request.json', {'seeds': ['any-seed'], 'upload': None})
    module = types.ModuleType('autofusex.fuzz.client.config')
    module.data_home = lambda: home
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(common, 'capture', lambda *a: '[["123",1]]')
    tasks = [row('cpu', backend='cpu'), row('mlir'), row('legacy', backend='af-legacy')]
    commands = []
    def invoke(argv):
        commands.append(argv)
        if argv[3] == 'replay':
            assert argv == [sys.executable, '-m', 'afx', 'replay', 'any-seed', '--data-dir', str(home)]
            return {'run_id': 'r', 'task_ids': [t['id'] for t in tasks]}
        if argv[4] == 'inspect':
            return {'state': 'running', 'spec': {'environment': 'actual'}}
        return {'attempt': attempt(next(t for t in tasks if t['attempt_id'] == argv[7]))}
    monkeypatch.setattr(m, 'invoke', invoke)
    monkeypatch.setattr(m, 'task_rows', lambda *a: tasks)
    m.execute(job)
    report = common.read(job / 'result.json')
    assert report['passed'] and report['execution_mode'] == 'managed'
    assert len(report['attempts']) == 3 and report['upload'] == 'managed'
    assert all(e['autofuse_binary'] == 'executed-binary' for e in report['execution_environments'].values())
    assert all('fuzz' not in argv for argv in commands)


def test_expected_backends_and_shape_policy():
    tasks = [row('cpu', backend='cpu'), row('mlir'), row('legacy', backend='af-legacy')]
    attempts = [attempt(t) for t in tasks]
    expected = {('123', a['backend']): 1 for a in attempts}
    assert replay.summarize(attempts, [['123', 3]], expected=expected)['passed']
    attempts[-1]['shape_results'][0]['correctness'] = 'fail'
    assert not replay.summarize(attempts, [['123', 3]], expected=expected)['passed']
    assert not replay.summarize([], [['123', 1]], expected={})['passed']
