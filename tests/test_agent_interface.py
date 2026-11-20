"""Agent response and retry contracts; no NPU or network needed."""

import af_forge.cli
import af_forge.cli as w
import shutil
from af_forge import common as workflow_common
from af_forge import execution as workflow_execution
from af_forge import jobs as workflow_jobs
from af_forge import source as workflow_source

import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import time
import pytest


def request(command="check"):
    return {
        "command": command,
        "seeds": [],
        "batch": False,
        "deployment": None,
        "upload": None,
    }


def test_same_request_reuses_frozen_snapshot_and_conflicts(cfg):
    first = workflow_jobs.submit(cfg, request("deploy"), "stable-id", True)
    source = Path(first["local"]) / "snapshot/old.cpp"
    assert source.read_text() == "old"
    (Path(cfg["local"]["source_dir"]) / "old.cpp").write_text("new edit")
    second = workflow_jobs.submit(cfg, request("deploy"), "stable-id", True)
    assert second["job"] == first["job"] and second["reused"]
    assert source.read_text() == "old"
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request("check"), "stable-id", True)
    assert error.value.code == "REQUEST_CONFLICT"


def test_concurrent_local_submission_has_one_job(cfg):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: workflow_jobs.submit(cfg, request(), "shared", True), range(2)
            )
        )
    assert results[0]["job"] == results[1]["job"]
    assert sorted(r["reused"] for r in results) == [False, True]


def test_lost_launch_reply_retries_without_upload_or_launch(cfg, monkeypatch):
    status = {"state": "not_found"}
    launches, uploads = [], []

    def query(c, job, action="status"):
        if action == "launch":
            launches.append(job)
            status.update(state="running", job=job)
            raise workflow_common.WorkflowError("REMOTE_UNAVAILABLE", "reply lost")
        return dict(status)

    monkeypatch.setattr(workflow_jobs, "query_job", query)
    monkeypatch.setattr(workflow_common, "run", lambda argv, **kw: uploads.append(argv))
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request(), "retry", False)
    assert error.value.data["request_id"] == "retry"
    result = workflow_jobs.submit(cfg, request(), "retry", False)
    assert result["state"] == "running"
    assert len(launches) == len(uploads) == 1
    assert result["job"] == error.value.data["job"]


def test_transfer_failure_retries_same_job(cfg, monkeypatch):
    uploads = []
    monkeypatch.setattr(
        workflow_jobs,
        "query_job",
        lambda c, job, action: {
            "state": "running" if action == "launch" else "not_found"
        },
    )

    def upload(argv, **kwargs):
        uploads.append(argv)
        if len(uploads) == 1:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(workflow_common, "run", upload)
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request(), "transfer", False)
    assert error.value.code == "TRANSFER_FAILED"
    result = workflow_jobs.submit(cfg, request(), "transfer", False)
    assert result["job"] == error.value.data["job"]
    assert uploads[0] == uploads[1]


def test_interrupted_preparation_never_reuses_partial_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(
        workflow_source,
        "snapshot",
        lambda *a: (_ for _ in ()).throw(OSError("interrupted")),
    )
    with pytest.raises(workflow_common.WorkflowError) as first:
        workflow_jobs.submit(cfg, request("deploy"), "partial", True)
    assert first.value.code == "PREPARATION_FAILED"
    with pytest.raises(workflow_common.WorkflowError) as retry:
        workflow_jobs.submit(cfg, request("deploy"), "partial", True)
    assert retry.value.code == "PREPARATION_INCOMPLETE"


def test_remote_concurrent_launch_and_uncertain_intent(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "af_forge").mkdir()
    (job / "af_forge/cli.py").write_text(
        "from pathlib import Path\np=Path(__file__).parent.parent/'starts'\nwith p.open('a') as f:f.write('start\\n')\n"
    )

    def launch():
        result = subprocess.run(
            [sys.executable, "-c", workflow_jobs.REMOTE_JOB, str(job), "launch"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: launch(), range(2)))
    for _ in range(100):
        if (job / "starts").exists():
            break
        time.sleep(0.02)
    assert (job / "starts").read_text() == "start\n"
    # A crash between writing the intent and recording the PID must not launch again.
    (job / "status.json").unlink()
    (job / "launch.json").write_text('{"state":"starting"}')
    assert launch()["state"] == "unknown"
    assert (job / "starts").read_text() == "start\n"


@pytest.mark.parametrize("use_default", [True, False])
def test_doctor_has_no_task_or_remote_side_effect(
    cfg, tmp_path, monkeypatch, capsys, use_default
):
    script = tmp_path / "tool" / "af_forge/cli.py"
    monkeypatch.setattr(w, "__file__", str(script))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = (
        tmp_path / ".config/af-forge/config.yaml"
        if use_default
        else tmp_path / "custom.yaml"
    )
    workflow_common.write(p, cfg)
    args = [] if use_default else ["--config", str(p)]
    monkeypatch.setattr(sys, "argv", ["af_forge/cli.py", "doctor", *args])
    calls = []
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *a: calls.append(a))
    monkeypatch.setattr(
        workflow_jobs,
        "query_job",
        lambda *a: pytest.fail("doctor must not connect to NPU"),
    )
    assert af_forge.cli.cli() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["data"]["login"] == "passed"
    assert calls == [("deploy", False)]
    assert not Path(cfg["local"]["state_dir"]).exists()


@pytest.mark.parametrize(
    "args,code",
    [
        ([], "INVALID_ARGUMENT"),
        (["doctor", "--config", "/missing/config.yaml"], "CONFIG_INVALID"),
        (
            ["check", "--config", "/missing/config.yaml", "--request-id", "bad id"],
            "INVALID_ARGUMENT",
        ),
    ],
)
def test_cli_errors_are_single_json_response(args, code):
    result = subprocess.run(
        [sys.executable, "-m", "af_forge.cli", *args], capture_output=True, text=True
    )
    assert result.returncode == 2
    envelope = json.loads(result.stdout)
    assert not envelope["ok"] and envelope["error"]["code"] == code


def test_auth_error_code_and_no_raw_service_output(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)

    def run(argv, **kw):
        if argv == ["npussh", "list"]:
            return subprocess.CompletedProcess(argv, 1, "", "尚未登录 secret-account")
        return subprocess.CompletedProcess(argv, 0, "v20.1.0", "")

    monkeypatch.setattr(workflow_common, "run", run)
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.local_preflight("check")
    assert error.value.code == "AUTH_REQUIRED"
    assert "secret-account" not in str(error.value)


def test_worker_phase_error_code(cfg, tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    workflow_common.write(job / "config.yaml", cfg)
    workflow_common.write(job / "request.json", request("deploy"))

    def fail(c, directory):
        workflow_common.phase(directory, "build")
        raise ValueError("build failed")

    monkeypatch.setattr(workflow_execution, "deploy", fail)
    assert workflow_jobs.worker(job) == 1
    assert workflow_common.read(job / "status.json")["error_code"] == "BUILD_FAILED"


def test_status_failure_is_successful_query(cfg, tmp_path, monkeypatch, capsys):
    p = tmp_path / "config.yaml"
    workflow_common.write(p, cfg)
    monkeypatch.setattr(
        sys, "argv", ["af_forge/cli.py", "status", "--config", str(p), "--job", "a" * 32]
    )
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(
        workflow_jobs,
        "query_status",
        lambda *a, **kw: {"state": "failed", "error_code": "REPLAY_FAILED"},
    )
    assert af_forge.cli.cli() == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] and envelope["data"]["state"] == "failed"
    assert envelope["data"]["error_code"] == "REPLAY_FAILED"


@pytest.mark.parametrize("output", ["noise", "[]", '{"state":"unexpected"}'])
def test_remote_protocol_failure_is_typed(cfg, monkeypatch, output):
    monkeypatch.setattr(
        workflow_common,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, output, ""),
    )
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.query_job(cfg, "a" * 32)
    assert error.value.code == "REMOTE_UNAVAILABLE"


@pytest.mark.parametrize(
    "remote_state", ["queued", "running", "success", "failed", "unknown"]
)
def test_local_snapshot_cleanup_requires_confirmed_launch(
    cfg, monkeypatch, remote_state
):
    monkeypatch.setattr(workflow_jobs, "query_job", lambda *a: {"state": remote_state})
    result = workflow_jobs.submit(cfg, request("deploy"), "cleanup", False)
    local = Path(result["local"])
    assert (local / "snapshot").exists() == (remote_state == "unknown")
    assert (local / "source.json").is_file()
    assert (local / "config.yaml").is_file()
    assert (local / "request.json").is_file()
    if remote_state != "unknown":
        assert result["local_snapshot_cleanup"]["state"] == "complete"
        saved = workflow_common.read(
            Path(cfg["local"]["state_dir"])
            / "requests"
            / (workflow_common.digest("cleanup") + ".json")
        )
        assert saved["remote_accepted"]
    retry = workflow_jobs.submit(cfg, request("deploy"), "cleanup", False)
    assert retry["job"] == result["job"] and retry["reused"]


def test_cleaned_submission_cannot_restart_missing_remote_job(cfg, monkeypatch):
    monkeypatch.setattr(workflow_jobs, "query_job", lambda *a: {"state": "success"})
    result = workflow_jobs.submit(cfg, request("deploy"), "retired", False)
    assert not (Path(result["local"]) / "snapshot").exists()
    monkeypatch.setattr(workflow_jobs, "query_job", lambda *a: {"state": "not_found"})
    monkeypatch.setattr(
        workflow_common, "run", lambda *a, **kw: pytest.fail("must not upload/relaunch")
    )
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request("deploy"), "retired", False)
    assert error.value.code == "REMOTE_TASK_LOST"


def test_status_cleans_snapshot_after_lost_launch_reply(cfg, monkeypatch):
    status = {"state": "not_found"}

    def query(c, job, action="status"):
        if action == "launch":
            status["state"] = "running"
            raise workflow_common.WorkflowError("REMOTE_UNAVAILABLE", "reply lost")
        return dict(status)

    original = workflow_common.run
    monkeypatch.setattr(workflow_jobs, "query_job", query)
    # Real Git capture is needed to freeze source; only mock upload.
    monkeypatch.setattr(
        workflow_common,
        "run",
        lambda argv, **kw: None if argv[0] == "npursync" else original(argv, **kw),
    )
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request("deploy"), "lost-reply", False)
    local = Path(error.value.data["local"])
    assert (local / "snapshot").exists()
    report = workflow_jobs.cleanup_after_status(cfg, local.name, status)
    assert report["state"] == "complete" and not (local / "snapshot").exists()


def test_upload_failure_retains_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(workflow_jobs, "query_job", lambda *a: {"state": "not_found"})
    original = workflow_common.run

    def fail_upload(argv, **kw):
        if argv[0] == "npursync":
            raise subprocess.CalledProcessError(1, argv)
        return original(argv, **kw)

    monkeypatch.setattr(workflow_common, "run", fail_upload)
    with pytest.raises(workflow_common.WorkflowError) as error:
        workflow_jobs.submit(cfg, request("deploy"), "upload-fail", False)
    assert error.value.code == "TRANSFER_FAILED"
    assert (Path(error.value.data["local"]) / "snapshot").is_dir()


def test_local_cleanup_error_keeps_acknowledgement_and_task_success(cfg, monkeypatch):
    monkeypatch.setattr(workflow_jobs, "query_job", lambda *a: {"state": "success"})
    record = (
        Path(cfg["local"]["state_dir"])
        / "requests"
        / (workflow_common.digest("cleanup-error") + ".json")
    )

    def fail(path):
        assert workflow_common.read(record)["remote_accepted"]
        raise PermissionError("busy")

    monkeypatch.setattr(shutil, "rmtree", fail)
    result = workflow_jobs.submit(cfg, request("deploy"), "cleanup-error", False)
    assert result["state"] == "success"
    assert result["local_snapshot_cleanup"]["state"] == "failed"
    assert (Path(result["local"]) / "snapshot").exists()


def test_status_exposes_cleanup_without_source_inventory(tmp_path):
    workflow_common.write(
        tmp_path / "status.json", {"state": "success", "job": tmp_path.name}
    )
    cleanup = {"state": "complete", "reason": "source_sync_verified"}
    workflow_common.write(
        tmp_path / "source.json",
        {"files": {"large-inventory": {}}, "snapshot_cleanup": cleanup},
    )
    result = subprocess.run(
        [sys.executable, "-c", workflow_jobs.REMOTE_JOB, str(tmp_path), "status"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["snapshot_cleanup"] == cleanup
    assert "files" not in data and "source.json" not in data


def test_submission_metadata_lives_in_request(cfg):
    result = workflow_jobs.submit(cfg, request(), "metadata", True)
    local = Path(result["local"])
    assert workflow_common.read(local / "request.json")["submission"][
        "request_key"
    ] == workflow_common.digest("metadata")
    assert not (local / "submission.json").exists()
    retry = workflow_jobs.submit(cfg, request(), "metadata", True)
    assert retry["job"] == result["job"] and retry["reused"]


@pytest.mark.parametrize("mode", ["init", "update"])
def test_env_command_transfers_frozen_config_and_runs_installer(cfg, monkeypatch, mode):
    from af_forge.environment import bootstrap
    monkeypatch.setattr(bootstrap, "prepare", lambda c, job: "/fixture/python3.11")
    monkeypatch.setattr(shutil, "which", lambda name: name)
    cfg["python_env"] = {"base_python": "/fixture/python3.11"}
    job = Path(workflow_jobs.submit(cfg, request(mode), "env-request", True)["local"])
    assert (job / "af_forge/environment/python.py").is_file() and (
        job / "af_forge/environment/cann.py"
    ).is_file()
    assert not (job / "snapshot").exists()

    def run(argv, **kwargs):
        assert kwargs["cwd"] == job
        assert argv == [
            "/fixture/python3.11",
            "-m",
            "af_forge.environment.python",
            f"_{mode}_job",
            job,
        ]
        assert workflow_common.read(job / "config.yaml") == cfg
        workflow_common.write(job / "result.json", {"state": "success"})

    monkeypatch.setattr(workflow_common, "run", run)
    assert workflow_jobs.worker(job) == 0
    assert workflow_common.read(job / "status.json")["artifact"] == str(
        job / "result.json"
    )


def test_init_status_identifies_failed_python_bootstrap(cfg, monkeypatch):
    from af_forge.environment import bootstrap
    cfg["python_env"] = {"base_python": "/missing/python"}
    job = Path(workflow_jobs.submit(cfg, request("init"), "missing-tool", True)["local"])
    def fail(*args):
        raise workflow_common.WorkflowError("PYTHON_BOOTSTRAP_FAILED", "sqlite>=3.35: actual 3.31")
    monkeypatch.setattr(bootstrap, "prepare", fail)
    assert workflow_jobs.worker(job) == 1
    status = workflow_common.read(job / "status.json")
    assert status["state"] == "failed" and status["phase"] == "python_bootstrap"
    assert status["error_code"] == "PYTHON_BOOTSTRAP_FAILED"
    assert "sqlite>=3.35" in status["error"]


def test_follow_streams_late_uv_log_and_stops_at_terminal_state(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    # A separate observer runs the same remote payload used by npussh.
    (job / "job.log").write_text("started toolkit\n")
    workflow_common.write(job / "status.json", {"state": "queued"})
    workflow_common.write(job / "launch.json", {"state": "starting"})
    # Keep the worker PID and job directory visible to the Linux /proc check.
    # Use a terminal status for the first synchronous read on non-Linux hosts.
    if not Path("/proc/self/cmdline").exists():
        workflow_common.write(
            job / "status.json", {"state": "failed", "phase": "cann_install"}
        )
        output = subprocess.check_output(
            [sys.executable, "-c", workflow_jobs.REMOTE_JOB, str(job), "follow"],
            text=True,
        )
        assert "failed cann_install" in output and "started toolkit" in output
        return
    with subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(10)", str(job)]
    ) as worker:
        try:
            workflow_common.write(job / "launch.json", {"pid": worker.pid})
            with subprocess.Popen(
                [sys.executable, "-c", workflow_jobs.REMOTE_JOB, str(job), "follow"],
                stdout=subprocess.PIPE,
                text=True,
            ) as observer:
                assert observer.stdout.readline().strip() == "[init] queued"
                (job / "uv.log").write_text("started uv\n")
                workflow_common.write(job / "status.json", {"state": "success"})
                output = observer.communicate(timeout=5)[0]
                assert (
                    output.count("started toolkit") == output.count("started uv") == 1
                )
                assert "[init] success" in output
        finally:
            worker.terminate()


def test_init_response_leaves_detailed_dependency_report_for_status():
    status = {
        "state": "success",
        "artifact": "/job/result.json",
        "log": "/job/job.log",
        "python-dependencies.json": {"checks": [{"stdout": "detailed probe output"}]},
    }
    data = {"job": "job", "request_id": "request", "state": "success", "status": status}
    assert af_forge.cli.response("init", data)["data"] == {
        "job_id": "job",
        "request_id": "request",
        "state": "success",
        "artifact": "/job/result.json",
        "log": "/job/job.log",
    }
    assert af_forge.cli.response("status", status)["data"] == status


@pytest.mark.parametrize(
    "args",
    [
        ["issue", "claim", "--request-id", "claim", "--seed", "123"],
        ["deploy", "--no-upload"],
        [
            "issue", "status",
            "--issue-id",
            "F152",
            "--round-no",
            "1",
            "--timeout",
            "3600",
        ],
        ["replay", "--job-id", "a" * 32, "--deployment", "/elsewhere", "--seed", "123"],
        ["replay", "--job-id", "../unsafe", "--seed", "123"],
        ["check", "--request-id", ""],
    ],
)
def test_invalid_arguments_stop_before_config_or_io(args, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["workflow", *args])
    monkeypatch.setattr(
        w.workflow_config, "config", lambda *a: pytest.fail("unexpected config read")
    )
    assert w.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_replay_resolves_deploy_job_in_configured_state_dir(cfg, monkeypatch):
    deploy_job = "a" * 32
    monkeypatch.setattr(
        sys, "argv", ["workflow", "replay", "--job-id", deploy_job, "--seed", "123"]
    )
    monkeypatch.setattr(w.workflow_config, "config", lambda path: cfg)
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(workflow_jobs, "submit", lambda c, request, *a: request)
    data = w.main()
    assert data["deployment"] == str(
        Path(cfg["remote"]["state_dir"]) / deploy_job / "deployment.json"
    )
    assert data["seeds"] == ["123"]


def test_claim_response_preserves_issue_id_and_empty_result():
    data = {"issue_id": "F152", "lease_id": "lease"}
    assert w.response("issue-claim", data)["data"]["issue_id"] == data["issue_id"]
    assert w.response("issue-claim", None)["data"] is None


def test_numbered_claim_displays_summary_and_keeps_full_receipt():
    summary = dict(
        issue_id="F152",
        status="claimed",
        title="compile error",
        backend="af-mlir",
        case_count=21,
        lease_expires_at=1800000000000,
        request_id="claim-F152",
    )
    receipt = dict(
        summary,
        id="F152",
        lease_id="lease",
        events=[{"kind": "claimed"}],
        repro={"cases": [{"case_key": "seed"}]},
        related_knowledge=[],
    )
    assert w.response("issue-claim", receipt)["data"] == summary
    assert receipt["repro"]["cases"] == [{"case_key": "seed"}]
