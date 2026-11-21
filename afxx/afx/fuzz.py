"""Thin adapter to official AFX fuzz commands; AFX owns scheduling and results."""

import contextlib
import fcntl
import json
from pathlib import Path

from afxx import common
from afxx.environment import runtime


def home(c):
    root = c.get("workflow_root") or str(Path(c["remote"]["state_dir"]).parent)
    return Path(root) / "fuzz"



@contextlib.contextmanager
def running(directory, job):
    """Reject concurrent starts; serialize ownership changes with control commands."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / ".afxx-run.lock").open("a+") as guard:
        with common.lock(directory / ".afxx-control.lock"):
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise common.WorkflowError("FUZZ_ALREADY_RUNNING", "another fuzz run owns " + str(directory)) from None
            # Update in place: replacing the file would invalidate the lock.
            guard.seek(0)
            guard.truncate()
            json.dump({"job": str(Path(job).resolve())}, guard)
            guard.flush()
        # Inherit this descriptor into the official foreground process so the
        # slot remains occupied even if the AFXX worker exits unexpectedly.
        yield guard.fileno()


def command(c, directory, options):
    argv = [runtime.python(c), "-m", "afx", "fuzz", "run", "--fg", "--save",
            "--data-dir", str(directory), "--database", str(directory / "results/results.sqlite")]
    for name in ("count", "workers", "compile_threads", "ops"):
        if options.get(name) is not None:
            argv += ["--" + name.replace("_", "-"), str(options[name])]
    for name in ("device", "backend"):
        for value in options.get(name) or []:
            argv += ["--" + name, value]
    return argv


def control(job, action):
    job = Path(job)
    common.require(action in ("status", "stop"), "invalid fuzz action")
    common.require_code((job / "request.json").is_file(), "JOB_NOT_FOUND", "fuzz job not found")
    common.require_code(common.read(job / "request.json")["command"] == "fuzz-run",
                        "INVALID_ARGUMENT", "job is not a fuzz run")
    c = common.read(job / "config.yaml")
    directory = home(c)
    common.require_code(directory.is_dir(), "FUZZ_NOT_STARTED", "fuzz has not started")
    with common.lock(directory / ".afxx-control.lock"):
        owner_file = directory / ".afxx-run.lock"
        common.require_code(owner_file.is_file(), "FUZZ_NOT_STARTED", "fuzz has not started")
        owner = common.read(owner_file)
        common.require_code(owner.get("job") == str(job.resolve()), "FUZZ_JOB_REPLACED",
                            "shared fuzz directory belongs to another job; use its job ID")
        common.require_code((directory / "tasks/tasks.sqlite").is_file(), "FUZZ_NOT_STARTED",
                            "AFX queue is not ready; query afxx status for job startup errors")
        env = dict(runtime.environment(c), AUTOFUSEX_HOME=str(directory))
        value = json.loads(common.capture(
            [runtime.python(c), "-m", "afx", "fuzz", action, "--data-dir", str(directory)],
            env=env, cwd=directory, timeout=60,
        ))
    return {"job_id": job.name, "results_dir": str(directory), "afx": value}
