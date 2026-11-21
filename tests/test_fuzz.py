"""Remote fuzz orchestration delegates scheduling and results to AFX."""
from pathlib import Path
import subprocess
import sys

import pytest

from afxx import cli, common, execution, jobs
from afxx.afx import fuzz
from afxx.environment import runtime
from afxx.job_view import status_view


def test_fuzz_cli_submits_without_source_snapshot(cfg, monkeypatch):
    monkeypatch.setattr(cli.workflow_config, "config", lambda _: cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["afxx", "fuzz", "run", "--count", "10", "--device", "npu:1",
                                     "--ops", "add,sum", "--dry-run", "--request-id", "fuzz-test"])
    result = cli.main()
    directory = Path(result["local"])
    request = common.read(directory / "request.json")
    assert request["command"] == "fuzz-run"
    assert request["fuzz"]["count"] == 10
    assert request["fuzz"]["device"] == ["npu:1"]
    assert not (directory / "snapshot").exists()
    assert (directory / "afxx/afx/fuzz.py").is_file()
    assert cli.main()["job"] == result["job"]


@pytest.mark.parametrize("code", [0, 2])
def test_fuzz_execution_uses_selected_environment_and_shared_results(cfg, tmp_path, monkeypatch, code):
    cfg["workflow_root"] = str(tmp_path / "workflow")
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(execution, "record_environment", lambda *a: {})
    monkeypatch.setattr(runtime, "environment", lambda _: {"VIRTUAL_ENV": cfg["runtime"]["venv_dir"], "ASCEND_HOME_PATH": "chosen-cann"})
    monkeypatch.setattr(runtime, "venv_preflight", lambda *a: None)
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(common, "run", run)
    request = {"fuzz": {"count": 10, "device": ["npu:0", "npu:1"], "workers": 2, "compile_threads": 3}}
    common.write(job / "config.yaml", cfg)
    common.write(job / "request.json", dict(request, command="fuzz-run"))
    monkeypatch.setattr(jobs.workflow_config, "validate_config", lambda value: value)
    assert jobs.worker(job) == (1 if code else 0)
    state = common.read(job / "status.json")
    assert state["state"] == ("failed" if code else "success")
    if code:
        assert state["error_code"] == "FUZZ_FAILED"
    directory = tmp_path / "workflow/fuzz"
    argv, options = seen[0]
    assert argv[:5] == [runtime.python(cfg), "-m", "afx", "fuzz", "run"]
    assert "--fg" in argv and "--save" in argv
    assert argv[argv.index("--database")+1] == str(directory / "results/results.sqlite")
    assert options["cwd"] == directory
    assert options["env"]["AUTOFUSEX_HOME"] == str(directory)
    assert options["env"]["ASCEND_HOME_PATH"] == "chosen-cann"
    assert options["env"]["VIRTUAL_ENV"] == cfg["runtime"]["venv_dir"]
    report = common.read(job / "result.json")
    assert report["exit_code"] == code
    assert not (directory / "results/results.sqlite").exists()  # Only AFX creates its database.
    view = status_view({"task_type": "fuzz-run", "result.json": report})
    assert view["fuzz"]["results_dir"] == str(directory)
    assert "replay" not in view


def test_fuzz_control_reuses_job_config_and_official_queue(cfg, tmp_path, monkeypatch):
    job = tmp_path / "job"
    common.write(job / "config.yaml", cfg)
    common.write(job / "request.json", {"command": "fuzz-run"})
    directory = fuzz.home(cfg)
    (directory / "tasks").mkdir(parents=True)
    (directory / "tasks/tasks.sqlite").touch()
    monkeypatch.setattr(runtime, "environment", lambda _: {})
    calls = []

    def capture(argv, **options):
        calls.append((argv, options))
        return '{"state":"stop_requested"}'

    monkeypatch.setattr(common, "capture", capture)
    with fuzz.running(directory, job):
        result = fuzz.control(job, "stop")
    assert result["afx"]["state"] == "stop_requested"
    assert calls[0][0] == [runtime.python(cfg), "-m", "afx", "fuzz", "stop", "--data-dir", str(directory)]
    assert calls[0][1]["env"]["AUTOFUSEX_HOME"] == str(directory)
    common.write(job / "request.json", {"command": "replay"})
    with pytest.raises(common.WorkflowError, match="not a fuzz"):
        fuzz.control(job, "stop")


def test_fuzz_count_rejected_before_submit(cfg, monkeypatch):
    monkeypatch.setattr(cli.workflow_config, "config", lambda _: cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(jobs, "submit", lambda *a: pytest.fail("invalid request submitted"))
    monkeypatch.setattr(sys, "argv", ["afxx", "fuzz", "run", "--count", "0"])
    with pytest.raises(common.WorkflowError, match="must be positive"):
        cli.main()


def test_shared_fuzz_slot_rejects_concurrency_and_stale_control(cfg, tmp_path, monkeypatch):
    directory = fuzz.home(cfg)
    first, second = tmp_path / "first", tmp_path / "second"
    for job in (first, second):
        common.write(job / "config.yaml", cfg)
        common.write(job / "request.json", {"command": "fuzz-run"})
    with fuzz.running(directory, first):
        # A separate process must see the same exclusion, not just a Python flag.
        script = "from pathlib import Path; import sys; from afxx.afx.fuzz import running; from afxx.common import WorkflowError\ntry:\n with running(Path(sys.argv[1]), Path(sys.argv[2])): sys.exit(1)\nexcept WorkflowError as e:\n assert e.code == 'FUZZ_ALREADY_RUNNING'"
        subprocess.run([sys.executable, "-c", script, str(directory), str(second)], check=True)
        assert common.read(directory / ".afxx-run.lock")["job"] == str(first.resolve())
    # Completion releases the slot; result files survive sequential runs.
    (directory / "results").mkdir()
    database = directory / "results/results.sqlite"
    database.write_bytes(b"existing results")
    with fuzz.running(directory, second):
        monkeypatch.setattr(common, "capture", lambda *a, **kw: pytest.fail("old job must not control new run"))
        with pytest.raises(common.WorkflowError) as error:
            fuzz.control(first, "stop")
        assert error.value.code == "FUZZ_JOB_REPLACED"
        assert database.read_bytes() == b"existing results"
    # Exceptions also release the slot.
    with pytest.raises(RuntimeError):
        with fuzz.running(directory, first):
            raise RuntimeError("failed startup")
    with fuzz.running(directory, second):
        pass


def test_fuzz_slot_survives_parent_release_until_child_exits(cfg, tmp_path):
    directory = fuzz.home(cfg)
    with fuzz.running(directory, tmp_path / "first") as fd:
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE, pass_fds=(fd,),
        )
    try:
        with pytest.raises(common.WorkflowError) as error:
            with fuzz.running(directory, tmp_path / "second"):
                pytest.fail("foreground process still holds the slot")
        assert error.value.code == "FUZZ_ALREADY_RUNNING"
    finally:
        child.communicate(timeout=10)
    with fuzz.running(directory, tmp_path / "second"):
        pass
