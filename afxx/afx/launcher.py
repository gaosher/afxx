"""Delegate resident client lifecycle to AFX 0.2.3 managed commands."""

import json
from pathlib import Path

from afxx import common as w
from afxx import config
from afxx.environment import runtime
from afxx.afx import replay


# Read credentials inside the selected interpreter, never in process arguments.
# Reserve stdout for the response even when AFX emits diagnostic messages.
MANAGED_START = r"""
import contextlib, json, sys, time
from pathlib import Path
settings = json.loads(sys.argv[1])
home = Path(settings['home'])
class ConfigurationMismatch(Exception):
    pass
try:
    with contextlib.redirect_stdout(sys.stderr):
        from autofusex.fuzz.daemon.management.modern_cli import run
        token = Path(settings['token_file']).read_text().strip()
        if not token or any(ch.isspace() for ch in token):
            raise ValueError('invalid client token file')
        path = home / 'client.json'
        if path.exists():
            client = json.loads(path.read_text())
            if (client.get('mode') != 'managed'
                or client.get('endpoint') != settings['endpoint']
                or client.get('devices') != settings['devices']
                or client.get('token') != token):
                raise ConfigurationMismatch()
        else:
            args = ['--data-dir', str(home), 'init', '--endpoint', settings['endpoint'], '--token', token]
            for device in settings['devices']:
                args += ['--device', device]
            run(args)
        session = run(['--data-dir', str(home), 'start'])
        ids = session.get('run_ids') or [session.get('run_id')]
        deadline = time.monotonic() + 60
        while True:
            status = run(['--data-dir', str(home), 'status'])
            states = {item['id']: item['state'] for item in status.get('runs', [])}
            if ids and all(states.get(identity) == 'running' for identity in ids):
                break
            if time.monotonic() >= deadline or any(states.get(identity) not in ('preparing', 'running') for identity in ids):
                raise RuntimeError('managed execution session did not become running')
            time.sleep(1)
    print(json.dumps({'session': session, 'status': status}))
except ConfigurationMismatch:
    print(json.dumps({'error': 'PLATFORM_CLIENT_MISMATCH'}))
except Exception as exc:
    print('AFX managed command failed: ' + type(exc).__name__, file=sys.stderr)
    # Upstream exceptions may contain HTTP headers or credentials.
    print(json.dumps({'error': 'PLATFORM_CLIENT_START_FAILED'}))
"""


def process_identity(pid):
    """Linux process start time protects against PID reuse."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def ensure_started(c, package):
    """Ensure a managed execution session exists before submitting verify.

    Package identity remains evidence owned by the caller; starting an existing
    session does not promise that a resident process refreshed loaded libraries.
    """
    settings = config.platform_settings(c)
    token = settings.get("client_token_file")
    w.require_code(bool(token), "CONFIG_INVALID", "fill platform.client_token_file (remote AFX client token)")
    home = Path(c["remote"]["state_dir"]).parent / "afx-client"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with w.lock(home / "launch.lock"):
        # Migration guard only: managed process ownership belongs entirely to AFX.
        if not (home / "client.json").exists():
            for name in ("launch.json", "platform-client.json"):
                path = home / name
                record = w.read(path) if path.exists() else {}
                w.require_code(
                    not process_identity(record.get("pid", 0)),
                    "PLATFORM_CLIENT_UNTRACKED",
                    f"stop the legacy AFX client before initializing managed client: {path}",
                )
        env = runtime.environment(c)
        env["AUTOFUSEX_HOME"] = str(home)
        devices = c["replay"]["devices"]
        if devices == "auto":
            count = replay.visible_device_count(
                w.capture(replay.device_command(runtime.python(c)), env=env, timeout=60)
            )
            devices = [f"npu:{i}" for i in range(count)]
        request = dict(home=str(home), token_file=token, endpoint=settings["url"], devices=devices)
        log_path = home / "client.log"
        try:
            with log_path.open("ab") as log:
                response = json.loads(w.capture(
                    [runtime.python(c), "-c", MANAGED_START, json.dumps(request)],
                    env=env, cwd=home, stderr=log, timeout=120,
                ))
        except Exception as error:
            raise w.WorkflowError("PLATFORM_CLIENT_START_FAILED", f"AFX managed client failed; see {log_path}") from error
        code = response.get("error")
        if code:
            message = ("managed client configuration differs; check endpoint, devices and client token"
                       if code == "PLATFORM_CLIENT_MISMATCH" else
                       f"AFX >= 0.2.3 managed startup failed; check installation, endpoint and token; see {log_path}")
            raise w.WorkflowError(code, message)
        w.require_code(
            response.get("session", {}).get("state") in ("running", "preparing"),
            "PLATFORM_CLIENT_START_FAILED", f"AFX execution session is not running; see {log_path}",
        )
        return dict(home=str(home), log=str(log_path), **response)
