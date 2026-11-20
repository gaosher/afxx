from pathlib import Path
import subprocess
import pytest
from af_forge import common as w
from af_forge.afx import launcher as l


@pytest.fixture
def launch_env(platform_cfg, monkeypatch):
    c = platform_cfg
    c["platform"]["client_token_file"] = "/tokens/client.token"
    c["replay"]["devices"] = ["npu:0", "npu:1"]
    monkeypatch.setattr(
        l.runtime,
        "environment",
        lambda c: {
            "VIRTUAL_ENV": c["runtime"]["venv_dir"],
            "ASCEND_HOME_PATH": c["runtime"]["install_root"],
        },
    )
    monkeypatch.setattr(
        l, "process_identity", lambda pid: "100" if pid == 123 else None
    )
    calls = []
    monkeypatch.setattr(l.w, "capture", lambda argv, **kw: calls.append((argv, kw)))

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        w.write(Path(kwargs["cwd"]) / "platform-client.json", {"pid": 123})
        return type("Process", (), {"pid": 123, "poll": lambda self: None})()

    monkeypatch.setattr(l.subprocess, "Popen", spawn)
    return c, calls


def test_official_cli_uses_selected_environment_and_is_reused(launch_env):
    c, calls = launch_env
    package = {"version": "9", "sha256": "a" * 64}
    assert l.ensure_started(c, package)["reused"] is False
    assert l.ensure_started(c, package)["reused"] is True
    assert len(calls) == 2
    argv, opts = calls[-1]
    assert argv[:6] == [l.runtime.python(c), "-m", "afx", "platform", "client", "run"]
    assert argv[-1] == "--retest" and argv.count("--device") == 2
    assert opts["start_new_session"] is True
    assert opts["env"]["VIRTUAL_ENV"] == c["runtime"]["venv_dir"]
    assert opts["env"]["ASCEND_HOME_PATH"] == c["runtime"]["install_root"]
    assert l.ensure_started(c, dict(package, sha256="b" * 64))["reused"] is True
    c["replay"]["devices"] = ["npu:0"]
    with pytest.raises(w.WorkflowError, match="another environment"):
        l.ensure_started(c, package)
    assert len(calls) == 2


def test_preflight_failure_never_starts(launch_env, monkeypatch):
    c, calls = launch_env

    def fail(*a, **k):
        raise subprocess.CalledProcessError(1, "afx")

    monkeypatch.setattr(l.w, "capture", fail)
    with pytest.raises(w.WorkflowError) as error:
        l.ensure_started(c, {})
    assert error.value.code == "PLATFORM_CLIENT_START_FAILED"
    assert not calls


def test_missing_client_token(launch_env):
    c, calls = launch_env
    del c["platform"]["client_token_file"]
    with pytest.raises(w.WorkflowError, match="platform.client_token_file"):
        l.ensure_started(c, {})
    assert not calls


def test_untracked_client_not_duplicated(launch_env):
    c, calls = launch_env
    home = Path(c["remote"]["state_dir"]).parent / "afx-client"
    w.write(home / "platform-client.json", {"pid": 123})
    with pytest.raises(w.WorkflowError) as error:
        l.ensure_started(c, {})
    assert error.value.code == "PLATFORM_CLIENT_UNTRACKED"
    assert not calls


def test_dead_client_relaunched(launch_env, monkeypatch):
    c, calls = launch_env
    l.ensure_started(c, {})
    home = Path(c["remote"]["state_dir"]).parent / "afx-client"
    old = w.read(home / "launch.json")
    old["start_time"] = "old"
    w.write(home / "launch.json", old)
    # Stale marker cannot identify a live matching process.
    w.write(home / "platform-client.json", {"pid": 999})
    assert l.ensure_started(c, {})["reused"] is False
    assert len(calls) == 4
