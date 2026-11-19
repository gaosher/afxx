#!/usr/bin/env python3
# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Shared errors, filesystem operations, locking and subprocess helpers."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid


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
            "job": job.name,
            "state": "running",
            "phase": name,
            "pid": os.getpid(),
            **extra,
        },
    )
