"""Deploy follows remote logs without changing the remote task lifecycle."""
import subprocess
import sys

import pytest

from afxx import cli, common, config, jobs


@pytest.fixture
def submitted(monkeypatch):
    monkeypatch.setattr(config, 'config', lambda *_: {'connection': {'environment': 'test-npu'}})
    monkeypatch.setattr(jobs, 'local_preflight', lambda *a: None)
    monkeypatch.setattr(jobs, 'submit', lambda *a: {
        'job': 'deploy-test', 'remote': '/state/deploy-test', 'state': 'queued'})
    monkeypatch.setattr(sys, 'argv', ['afxx', 'deploy'])
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {'state': 'success'})


def test_deploy_follows_logs_to_stderr(submitted, monkeypatch, capsys):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs['stdout'] is sys.stderr
        assert kwargs['stderr'] is sys.stderr
        print('remote build log', file=kwargs['stdout'])
    monkeypatch.setattr(common, 'run', run)
    assert cli.main()['state'] == 'success'
    assert calls[0][:2] == ['npussh', 'test-npu']
    assert '/state/deploy-test follow deploy' in calls[0][2]
    output = capsys.readouterr()
    assert not output.out
    assert 'remote build log' in output.err


@pytest.mark.parametrize('option', ['--detach', '--dry-run'])
def test_deploy_can_return_without_following(submitted, monkeypatch, option):
    monkeypatch.setattr(sys, 'argv', ['afxx', 'deploy', option])
    monkeypatch.setattr(common, 'run', lambda *a, **kw: pytest.fail('must not follow'))
    assert cli.main()['state'] == 'queued'


def test_deploy_returns_remote_failure(submitted, monkeypatch):
    monkeypatch.setattr(common, 'run', lambda *a, **kw: None)
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {
        'state': 'failed', 'error_code': 'BUILD_FAILED', 'error': 'link failed'})
    with pytest.raises(common.WorkflowError) as caught:
        cli.main()
    assert caught.value.code == 'BUILD_FAILED'
    assert str(caught.value) == 'link failed'


@pytest.mark.parametrize('exception,code', [
    (KeyboardInterrupt(), 'FOLLOW_INTERRUPTED'),
    (subprocess.CalledProcessError(1, 'npussh'), 'REMOTE_UNAVAILABLE'),
])
def test_follow_interruption_does_not_cancel_deploy(submitted, monkeypatch, exception, code):
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        raise exception
    monkeypatch.setattr(common, 'run', run)
    with pytest.raises(common.WorkflowError) as caught:
        cli.main()
    assert caught.value.code == code
    assert len(calls) == 1
