"""Replay uploads use the official one-shot platform client, never a daemon."""
import json
from pathlib import Path
import subprocess

import pytest

from afxx import common, execution
from afxx.environment import runtime
from test_replay_adapter import results


@pytest.mark.parametrize("mode", ["success", "command-failed", "launch-failed", "invalid-receipt", "disabled", "failed-replay"])
def test_replay_upload_receipt_and_failure_evidence(platform_cfg, tmp_path, monkeypatch, mode):
    c = platform_cfg
    c["upload"]["enabled"] = True
    c["platform"]["client_token_file"] = str(tmp_path / "client.token")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(runtime, "current_deployment", lambda *a: {"job": "deploy", "runtime": {}})
    monkeypatch.setattr(execution, "record_environment", lambda *a: {"af": {}})
    monkeypatch.setattr(runtime, "environment", lambda *a: {"SELECTED_CANN": "yes"})
    monkeypatch.setattr(runtime, "venv_preflight", lambda *a: None)
    monkeypatch.setattr(common, "capture", lambda *a, **kw: json.dumps([["123", 3]]))
    monkeypatch.setattr(execution.replay_adapter, "read_results", lambda *a: results())
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["SELECTED_CANN"] == "yes"
        if "once" in argv:
            if mode == "launch-failed":
                raise OSError("cannot launch uploader")
            assert argv[:6] == [runtime.python(c), "-m", "afx", "platform", "client", "once"]
            assert argv[argv.index("--endpoint")+1] == c["platform"]["url"]
            assert argv[argv.index("--token-file")+1] == c["platform"]["client_token_file"]
            assert "--retest" not in argv
            assert argv[argv.index("--results")+1] == str(Path(c["replay"]["results_dir"]) / "job/results.sqlite")
            assert argv[argv.index("--state")+1].endswith("/job/platform-sync.sqlite")
            body = "not JSON" if mode == "invalid-receipt" else json.dumps({"uploaded": 2, "client_id": "client", "update_to": "9.9"})
            return subprocess.CompletedProcess(argv, 2 if mode == "command-failed" else 0, stdout=body)
        Path(argv[argv.index("--database")+1]).touch()
        return subprocess.CompletedProcess(argv, 1 if mode == "failed-replay" else 0)

    monkeypatch.setattr(common, "run", run)
    request = {"deployment": "unused", "seeds": ["123"], "batch": False,
               "upload": False if mode == "disabled" else None}
    if mode == "disabled":
        del c["platform"]  # --no-upload does not require platform credentials.
    if mode in ("command-failed", "launch-failed", "invalid-receipt", "failed-replay"):
        with pytest.raises(ValueError):
            execution.replay(c, job, request)
    else:
        execution.replay(c, job, request)
    report = common.read(job / "result.json")
    assert len(calls) == (1 if mode == "disabled" else 2)
    assert "evidence" in report and "attempts" in report
    assert not (Path(c["replay"]["results_dir"]) / "job/sync.json").exists()
    assert "sync_start_exit_code" not in report
    if mode == "disabled":
        assert report["upload"] == "disabled"
    elif mode in ("command-failed", "launch-failed", "invalid-receipt"):
        assert report["upload"] == "failed" and report["state"] == "failed"
    else:
        assert report["upload"] == "completed"
        assert report["upload_receipt"]["uploaded"] == 2
        assert report["state"] == ("failed" if mode == "failed-replay" else "success")
