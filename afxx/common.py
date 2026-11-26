#!/usr/bin/env python3
"""Shared errors, filesystem operations, locking and subprocess helpers."""

import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import uuid


# Safe single directory component; requests is the local idempotency-record directory.
JOB_ID_PATTERN = r"^(?!\.{1,2}$)(?!requests$)[A-Za-z0-9_.-]{1,128}$"


def validate_job_id(value):
    require_code(isinstance(value, str) and re.fullmatch(JOB_ID_PATTERN, value),
                 "INVALID_ARGUMENT", "job/request ID must be 1–128 letters, digits, _, . or -; '.', '..' and 'requests' are reserved")
    return value


def grouped_path(request, execution):
    """Remote relative directory; allocation remains protected by the local group lock."""
    kind = request.get("command")
    if kind not in ("deploy", "replay") or "run" not in request:
        return validate_job_id(execution)
    group = validate_job_id(request.get("request_id"))
    run = request["run"]
    require_code(type(run) is int and run > 0, "INVALID_ARGUMENT", "run must be a positive integer")
    return f"{group}/{kind}/run-{run}"


def remote_job(c, execution):
    validate_job_id(execution)
    record = Path(c["local"]["state_dir"]) / "requests" / (digest(execution) + ".json")
    relative = read(record).get("remote_relative", execution) if record.exists() else execution
    parts = Path(relative).parts
    require_code(not Path(relative).is_absolute() and all(part not in (".", "..") for part in parts),
                 "INVALID_ARGUMENT", "invalid remote task path")
    return Path(c["remote"]["state_dir"]) / relative


def execution_id(job):
    request = job / "request.json"
    return read(request).get("execution_id", job.name) if request.exists() else job.name


PROTECTED = {
    ".git",
    ".agents",
    ".codex",
    ".claude",
    ".aws",
    ".ssh",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "build",
    "build_out",
    "output",
    "dist",
    "__pycache__",
    ".pytest_cache",
    ".artifacts",
    ".deps",
    "open_source",
    ".af-workflow",
}


class WorkflowError(ValueError):
    def __init__(self, code, message, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


def require_code(condition, code, message):
    if not condition:
        raise WorkflowError(code, message)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "duplicate JSON key: " + key)
        result[key] = value
    return result


def read(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=pairs)


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def update_record(path, **fields):
    """Atomically checkpoint fields without discarding earlier stage evidence."""
    path = Path(path)
    value = read(path) if path.exists() else {}
    value.update(fields)
    write(path, value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(path, parent):
    return path == parent or parent in path.parents


def relative(value):
    require(
        isinstance(value, str) and value and "\x00" not in value,
        "invalid relative path",
    )
    p = Path(value)
    require(
        not p.is_absolute() and ".." not in p.parts and value != ".",
        "unsafe relative path: " + value,
    )
    return p


def protected(value):
    p = relative(value)
    return any(
        x in PROTECTED or x.startswith(".env") or x.endswith((".pem", ".key"))
        for x in p.parts
    )


def run(argv, check=True, timeout=None, **kwargs):
    command = [str(x) for x in argv]
    child = subprocess.Popen(command, start_new_session=True, **kwargs)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        raise
    result = subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result


def capture(argv, **kwargs):
    return run(argv, stdout=subprocess.PIPE, text=True, **kwargs).stdout


@contextlib.contextmanager
def lock(path, shared=False):
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def phase(job, name, **extra):
    write(
        job / "status.json",
        {
            "job": execution_id(job),
            "state": "running",
            "phase": name,
            "pid": os.getpid(),
            **extra,
        },
    )
