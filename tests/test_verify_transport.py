import contextlib
import json
import shlex
import subprocess
import sys

import pytest
from afxx import common, jobs, verify_transport
from afxx.afx import platform
from afxx.environment import runtime


def test_local_submit_reuses_key_and_uses_local_token_path(tmp_path, monkeypatch):
    c = {'local': {'state_dir': str(tmp_path)}}
    package = {'version': '9.3', 'sha256': 'a' * 64}
    lease = {'id': 'F2808', 'lease_id': 'lease'}
    held = []
    @contextlib.contextmanager
    def probe(c):
        held.append(True)
        try:
            yield package
        finally:
            held.pop()
    monkeypatch.setattr(verify_transport, 'installed_package', probe)
    monkeypatch.setattr(jobs, 'client_control', lambda *a: None)
    calls = []
    def post(c, method, path, body, **kw):
        assert held and kw == {'remote': False}
        calls.append(dict(body))
        if len(calls) == 1:
            raise common.WorkflowError('PLATFORM_UNAVAILABLE', 'lost response')
        return {'issue_id': 'F2808', 'round_no': 2}
    monkeypatch.setattr(platform, 'call', post)
    with pytest.raises(common.WorkflowError):
        jobs.verify_current(c, 'repair', lease)
    first = jobs.verify_current(c, 'repair', lease)
    assert jobs.verify_current(c, 'repair', lease) == first
    assert calls[0] == calls[1] and len(calls) == 2
    assert not held


@pytest.mark.parametrize('fail', [False, True])
def test_remote_probe_holds_locks_until_caller_finishes(tmp_path, monkeypatch, fail):
    import fcntl
    venv = tmp_path / 'venv'
    (venv / 'bin').mkdir(parents=True)
    (venv / 'bin/python').symlink_to(sys.executable)
    (venv / 'bin/activate').write_text(':\n')
    cann = tmp_path / 'Ascend/cann'
    cann.mkdir(parents=True)
    fake = tmp_path / 'fake'
    (fake / 'afx').mkdir(parents=True)
    (fake / 'afx/__init__.py').write_text('')
    (fake / 'afx/__main__.py').write_text("import json; print(json.dumps({'data':{'environment':{'cann_version':'9.3','pyautofuse_sha256':'a'*64}}}))")
    (cann / 'set_env.sh').write_text('export PYTHONPATH=' + shlex.quote(str(fake)) + '\n')
    c = {'connection': {'environment': 'test'}, 'runtime': {'venv_dir': str(venv), 'install_root': str(cann.parent)},
         'build': {'llvm_build_dir': '/tmp', 'lld_bin': '/usr/bin/ld'}}
    popen = subprocess.Popen
    def local(argv, **kw):
        assert argv[0] == 'npussh' and len(argv[2]) < 8192
        return popen(shlex.split(argv[2]), **kw)
    monkeypatch.setattr(verify_transport.subprocess, 'Popen', local)
    lock = runtime.registry(c).with_suffix('.lock')
    def operation():
        with verify_transport.installed_package(c) as package:
            assert package == {'version': '9.3', 'sha256': 'a' * 64}
            with lock.open('a') as stream:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if fail:
                raise RuntimeError('post failed')
    if fail:
        with pytest.raises(RuntimeError):
            operation()
    else:
        operation()
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
