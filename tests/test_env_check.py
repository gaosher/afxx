"""Direct environment reports and short environment operation identities."""
import hashlib
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
from afxx import common, jobs, mcp_operations
from afxx.environment import runtime, snapshot


@pytest.mark.parametrize('passed', [True, False])
def test_report_does_not_write_task_files(monkeypatch, passed):
    monkeypatch.setattr(snapshot, 'capture', lambda c: {'errors': []})
    monkeypatch.setattr(runtime, 'environment', lambda c: {})
    monkeypatch.setattr(common, 'write', lambda *a: pytest.fail('no task files'))
    dependencies = {'passed': passed, 'checks': [{'name': 'torch', 'passed': passed}]}
    monkeypatch.setattr(common, 'capture', lambda *a, **kw: json.dumps(dependencies))
    if passed:
        report = snapshot.inspect_environment({'runtime': {'venv_dir': '/venv'}})
    else:
        with pytest.raises(common.WorkflowError) as error:
            snapshot.inspect_environment({'runtime': {'venv_dir': '/venv'}})
        report = error.value.data
    assert report['dependencies'] == dependencies
    assert 'request_id' not in report


def test_remote_adapter_returns_failure_and_cleans_code(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'local_preflight', lambda *a: None)
    c = {'connection': {'environment': 'test'}, 'runtime': {
        'venv_dir': str(tmp_path/'venv'), 'install_root': str(tmp_path/'missing')}}
    def capture(argv, **kwargs):
        remote = shlex.split(argv[2])
        remote[0] = sys.executable
        # Execute the actual packaged bootstrap locally, with an isolated temp root.
        import os
        env = dict(os.environ, TMPDIR=str(tmp_path))
        result = subprocess.run(remote, text=True, capture_output=True, env=env, stdin=kwargs["stdin"])
        assert result.returncode == 0, result.stderr
        assert not list(tmp_path.iterdir())
        return result.stdout
    monkeypatch.setattr(common, 'capture', capture)
    with pytest.raises(common.WorkflowError) as error:
        jobs.environment_check(c)
    assert error.value.code == 'ENV_CHECK_FAILED'
    assert error.value.data['errors']


def test_mcp_check_has_no_operation_receipt(monkeypatch):
    from test_mcp_tools import call
    from afxx import mcp_server
    monkeypatch.setattr(mcp_operations, 'submit', lambda *a: pytest.fail('no operation'))
    async def invoke(args):
        assert args == ['env', 'check', '--config', '/config']
        return 0, json.dumps({'ok': True, 'data': {'state': 'success'}, 'error': None})
    monkeypatch.setattr(mcp_server, 'invoke', invoke)
    result = call(mcp_server.create_server('/config'), 'afxx_run', {'command': 'env check'})
    assert result.structuredContent['data'] == {'state': 'success'}


def test_short_id_skips_local_and_remote_collisions(tmp_path, monkeypatch):
    state = tmp_path/'state'
    keys = iter(['a'*32, 'b'*32, 'c'*32])
    monkeypatch.setattr(jobs.uuid, 'uuid4', lambda: SimpleNamespace(hex=next(keys)))
    common.write(state/'requests'/(common.digest('ENV-INIT-'+'a'*12)+'.json'), {})
    observed = []
    def query(c, argv):
        key = __import__('pathlib').Path(argv[-1]).name
        observed.append(key)
        return {'state': 'success' if key.endswith('b'*12) else 'not_found'}
    monkeypatch.setattr(jobs, 'remote', query)
    monkeypatch.setattr(jobs, 'submit', lambda c,r,key,d: key)
    assert jobs.submit_environment({'local': {'state_dir': str(state)}, 'remote': {'state_dir': '/remote'}}, {'command': 'init'}, False) == 'ENV-INIT-'+'c'*12
    assert observed == ['ENV-INIT-'+'b'*12, 'ENV-INIT-'+'c'*12]


def test_mcp_short_id_collision(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_operations, 'root', lambda: tmp_path)
    monkeypatch.setattr(mcp_operations, '_CHILDREN', [])
    keys = iter(['a'*32, 'b'*32])
    monkeypatch.setattr(mcp_operations.uuid, 'uuid4', lambda: SimpleNamespace(hex=next(keys)))
    (tmp_path/hashlib.sha256(('ENV-UPDATE-'+'a'*12).encode()).hexdigest()).mkdir()
    monkeypatch.setattr(mcp_operations, 'write', lambda p,v: p.write_text(json.dumps(v)))
    monkeypatch.setattr(mcp_operations.subprocess, 'Popen', lambda *a, **kw: SimpleNamespace(pid=1))
    monkeypatch.setattr(mcp_operations, 'status', lambda key: key)
    assert mcp_operations.submit('env update', [], None, None) == 'ENV-UPDATE-'+'b'*12
