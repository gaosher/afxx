"""Remote adapters must not place bundles or request data in process arguments."""
import json
import shlex
import subprocess
import sys

import pytest

from afxx import common, jobs


@pytest.mark.parametrize('failure', [False, True])
def test_large_payload_roundtrip_and_error_passthrough(monkeypatch, failure):
    value = '中文\n\" $(must-not-execute) ' * 100000
    handles = []

    def capture(argv, **kwargs):
        assert len(shlex.join(argv)) < 2048
        assert value not in shlex.join(argv)
        handles.append(kwargs['stdin'])
        result = subprocess.run(shlex.split(argv[2]), stdin=kwargs['stdin'],
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout

    monkeypatch.setattr(common, 'capture', capture)
    bootstrap = """import json,sys
value=json.loads(sys.argv[2])
result={'error':{'code':'LEASE_EXPIRED','message':value}} if sys.argv[3]=='fail' else {'data':value}
print(json.dumps(result))
"""
    c = {'connection': {'environment': 'test'}}
    monkeypatch.setattr(jobs.workflow_runtime, 'python', lambda c: sys.executable)
    args = [json.dumps(value), 'fail' if failure else 'ok']
    if failure:
        with pytest.raises(common.WorkflowError) as error:
            jobs.adapter_call(c, ('__init__.py',), bootstrap, args, 30, 'unavailable')
        assert error.value.code == 'LEASE_EXPIRED'
        assert str(error.value) == value
    else:
        assert jobs.adapter_call(c, ('__init__.py',), bootstrap, args, 30, 'unavailable') == value
    assert handles[0].closed


def test_transport_failure_closes_payload(monkeypatch):
    handles = []
    def capture(argv, **kwargs):
        handles.append(kwargs['stdin'])
        raise OSError('connection closed')
    monkeypatch.setattr(common, 'capture', capture)
    c = {'connection': {'environment': 'test'}, 'runtime': {'venv_dir': '/venv'}}
    with pytest.raises(common.WorkflowError) as error:
        jobs.adapter_call(c, ('__init__.py',), '', [], 30, 'query before retry')
    assert error.value.code == 'REMOTE_UNAVAILABLE'
    assert str(error.value) == 'query before retry'
    assert handles[0].closed
