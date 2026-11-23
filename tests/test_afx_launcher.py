import json
from pathlib import Path
import subprocess
import pytest
from afxx import common as w
from afxx.afx import launcher as l


@pytest.fixture
def launch_env(platform_cfg, monkeypatch):
    monkeypatch.setattr(l, "selected_client", lambda: None)
    c = platform_cfg
    c["platform"]["client_token_file"] = "/tokens/client.token"
    c["replay"]["devices"] = ["npu:0", "npu:1"]
    monkeypatch.setattr(l.runtime, "environment", lambda c: {"VIRTUAL_ENV": c["runtime"]["venv_dir"]})
    monkeypatch.setattr(l, "process_identity", lambda pid: "100" if pid == 123 else None)
    monkeypatch.setattr(l, "client_home", lambda c: Path(c["remote"]["state_dir"]).parent / "afx-client")
    calls = []
    def capture(argv, **kw):
        calls.append((argv, kw))
        return json.dumps({"session": {"state": "preparing", "run_id": "r"}, "status": {"runs": [{"id": "r", "state": "running"}]}})
    monkeypatch.setattr(l.w, "capture", capture)
    return c, calls


def test_managed_start_uses_selected_environment(launch_env):
    c, calls = launch_env
    result = l.ensure_started(c, {})
    argv, opts = calls[0]
    assert argv[:2] == [l.runtime.python(c), "-c"]
    request = json.loads(argv[-1])
    assert request["token_file"] == "/tokens/client.token"
    assert request["devices"] == ["npu:0", "npu:1"]
    assert opts["env"]["VIRTUAL_ENV"] == c["runtime"]["venv_dir"]
    assert result["status"]["runs"][0]["state"] == "running"
    assert not (Path(result["home"]) / "launch.json").exists()


@pytest.mark.parametrize("code", ["PLATFORM_CLIENT_START_FAILED", "PLATFORM_CLIENT_MISMATCH"])
def test_managed_failure_is_propagated(launch_env, monkeypatch, code):
    c, _ = launch_env
    monkeypatch.setattr(l.w, "capture", lambda *a, **k: json.dumps({"error": code}))
    with pytest.raises(w.WorkflowError) as error:
        l.ensure_started(c, {})
    assert error.value.code == code


def test_existing_client_can_reuse_persisted_token(launch_env):
    c, calls = launch_env
    del c["platform"]["client_token_file"]
    l.ensure_started(c, {})
    assert json.loads(calls[0][0][-1])["token_file"] is None


@pytest.mark.parametrize("name", ["launch.json", "platform-client.json"])
def test_legacy_client_not_duplicated(launch_env, name):
    c, calls = launch_env
    home = Path(c["remote"]["state_dir"]).parent / "afx-client"
    w.write(home / name, {"pid": 123})
    with pytest.raises(w.WorkflowError) as error:
        l.ensure_started(c, {})
    assert error.value.code == "PLATFORM_CLIENT_UNTRACKED"
    assert not calls


def test_invalid_response_fails_closed(launch_env, monkeypatch):
    c, _ = launch_env
    monkeypatch.setattr(l.w, "capture", lambda *a, **k: "native log\n{}")
    with pytest.raises(w.WorkflowError) as error:
        l.ensure_started(c, {})
    assert error.value.code == "PLATFORM_CLIENT_CONTROL_FAILED"


@pytest.mark.parametrize('action', ['status', 'start', 'stop', 'restart'])
def test_existing_live_home_wins_and_needs_no_platform_config(launch_env, monkeypatch, tmp_path, action):
    c, calls = launch_env
    home = tmp_path/'live'
    home.mkdir()
    (home/'client.json').write_text('{}')
    c.pop('platform')
    monkeypatch.setattr(l, 'selected_client', lambda: {'home': str(home), 'pid': 12})
    monkeypatch.setattr(l, 'client_home', lambda c: pytest.fail('must use live home'))
    result = l.control(c, action)
    assert result['home'] == str(home)
    request = json.loads(calls[0][0][-1])
    assert request['home'] == str(home)
    assert request['token_file'] is None and request['devices'] is None


def test_no_live_restart_does_not_resolve_or_create_home(launch_env, monkeypatch):
    c, calls = launch_env
    monkeypatch.setattr(l, 'client_home', lambda c: pytest.fail('must not resolve home'))
    with pytest.raises(w.WorkflowError) as error:
        l.control(c, 'restart')
    assert error.value.code == 'PLATFORM_CLIENT_NOT_RUNNING'
    assert not calls
