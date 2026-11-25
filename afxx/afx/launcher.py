"""Delegate resident client lifecycle to AFX 0.4.1 managed commands."""

import contextlib
import os
import sys
import json
from pathlib import Path

from afxx import common as w
from afxx import config
from afxx.environment import runtime
from afxx.afx import replay
from afxx.afx.managed_client import selected_client, ClientError


# Read credentials inside the selected interpreter, never in process arguments.
# Reserve stdout for the response even when AFX emits diagnostic messages.
MANAGED_START = Path(__file__).with_name("managed_client.py").read_text()


def process_identity(pid):
    """Linux process start time protects against PID reuse."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def client_home(c):
    """Resolve the remote AFX home without overriding its environment or pointer."""
    code = "import json; from autofusex.client.paths import data_home; print(json.dumps(str(data_home())))"
    try:
        return Path(json.loads(w.capture([runtime.python(c), "-c", code], timeout=30)))
    except Exception as error:
        raise w.WorkflowError("PLATFORM_CLIENT_CONTROL_FAILED", "cannot resolve AFX client home using configured Python") from error


def control(c, action, timeout=300, *, home=None, lock_held=False, daemon_only=False, inspect=False):
    """Control the one selected AFX home without replacing its identity."""
    w.require_code(action in ("status", "start", "stop", "restart"), "INVALID_ARGUMENT", "invalid client action")
    try:
        selected = selected_client()
    except ClientError as error:
        raise w.WorkflowError(error.code, str(error)) from None
    if not selected and action == "restart":
        raise w.WorkflowError("PLATFORM_CLIENT_NOT_RUNNING", "no live client to restart; use client start")
    if not selected and action in ("status", "stop"):
        return dict(state="stopped", daemon_running=False, home=None, runs=[], conflicts=[], retest_readiness="not_checked")
    if home is not None:
        home = Path(home)
        w.require_code(not selected or Path(selected["home"]).resolve() == home.resolve(),
                       "PLATFORM_CLIENT_CONFLICT", "client home changed during deployment")
    else:
        home = Path(selected["home"]) if selected else client_home(c)
    initialize = action == "start" and not (home / "client.json").exists()
    settings = config.platform_settings(c) if initialize else {}
    token = settings.get("client_token_file")
    if action != "status":
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.nullcontext() if action == "status" or lock_held else w.lock(home / "launch.lock"):
        # Migration guard only: managed process ownership belongs entirely to AFX.
        if action in ("start", "restart") and not (home / "client.json").exists():
            for name in ("launch.json", "platform-client.json"):
                path = home / name
                record = w.read(path) if path.exists() else {}
                w.require_code(
                    not process_identity(record.get("pid", 0)),
                    "PLATFORM_CLIENT_UNTRACKED",
                    f"stop the legacy AFX client before initializing managed client: {path}",
                )
        env = runtime.environment(c) if action in ("start", "restart") else dict(os.environ)
        env["AUTOFUSEX_HOME"] = str(home)
        devices = c["replay"]["devices"] if initialize else None
        if initialize and devices == "auto":
            count = replay.visible_device_count(
                w.capture(replay.device_command(runtime.python(c)), env=env, timeout=60)
            )
            devices = [f"npu:{i}" for i in range(count)]
        request = dict(home=str(home), token_file=token, endpoint=settings.get("url"), devices=devices, action=action, timeout=timeout, daemon_only=daemon_only, inspect=inspect)
        log_path = home / "client.log"
        try:
            with contextlib.nullcontext(sys.stderr) if action == "status" else log_path.open("ab") as log:
                response = json.loads(w.capture(
                    [runtime.python(c), "-c", MANAGED_START, json.dumps(request)],
                    env=env, cwd=home if home.exists() else None, stderr=log, timeout=timeout + 30,
                ))
        except Exception as error:
            raise w.WorkflowError("PLATFORM_CLIENT_CONTROL_FAILED", f"AFX managed client response unavailable; query client status; see {log_path}") from error
        code = response.get("error")
        if code:
            raise w.WorkflowError(code, response.get("message", f"AFX client action failed; see {log_path}"))
        return dict(home=str(home), log=str(log_path), **response)


def ensure_started(c, package):
    return control(c, "start")
