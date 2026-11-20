"""Launch the official AFX CLI; no upload, heartbeat or retest implementation."""

import os
from pathlib import Path
import subprocess
import time

from af_forge import common as w
from af_forge import config
from af_forge.environment import runtime
from af_forge.afx import replay


def process_identity(pid):
    """Linux process start time protects against PID reuse."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, ValueError, IndexError):
        return None


def ensure_started(c, package):
    settings = config.platform_settings(c)
    token = settings.get("client_token_file")
    w.require_code(
        bool(token),
        "CONFIG_INVALID",
        "fill platform.client_token_file (remote AFX client token)",
    )
    home = Path(c["remote"]["state_dir"]).parent / "afx-client"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with w.lock(home / "launch.lock"):
        env = runtime.environment(c)
        env["AUTOFUSEX_HOME"] = str(home)
        devices = c["replay"]["devices"]
        if devices == "auto":
            count = replay.visible_device_count(
                w.capture(replay.device_command(runtime.python(c)), env=env, timeout=60)
            )
            devices = [f"npu:{i}" for i in range(count)]
        (home / "results").mkdir(exist_ok=True, mode=0o700)
        base = [runtime.python(c), "-m", "afx", "platform", "client"]
        options = [
            "--endpoint",
            settings["url"],
            "--token-file",
            token,
            "--results",
            str(home / "results/results.sqlite"),
        ]
        for device in devices:
            options.extend(["--device", device])
        argv = base + ["run", *options, "--retest"]
        expected = {
            "argv": argv,
            "package": package,
            "environment": {
                k: env.get(k)
                for k in (
                    "VIRTUAL_ENV",
                    "ASCEND_HOME_PATH",
                    "LD_LIBRARY_PATH",
                    "PYTHONPATH",
                    "ASCEND_RT_VISIBLE_DEVICES",
                    "ASCEND_VISIBLE_DEVICES",
                )
            },
        }
        record_path = home / "launch.json"
        marker_path = home / "platform-client.json"
        record = w.read(record_path) if record_path.exists() else {}
        active = record and process_identity(record["pid"]) == record["start_time"]
        if active:
            w.require_code(
                record["expected"] == expected,
                "PLATFORM_CLIENT_MISMATCH",
                f"running AFX client uses another environment or AF package; see {record_path}",
            )
            w.require_code(
                marker_path.exists()
                and w.read(marker_path).get("pid") == record["pid"],
                "PLATFORM_CLIENT_START_FAILED",
                f"AFX startup not confirmed; see {home / 'client.log'}",
            )
            return {
                "pid": record["pid"],
                "reused": True,
                "log": str(home / "client.log"),
            }
        # Do not duplicate an independently started client in the same AFX home.
        if marker_path.exists():
            marker = w.read(marker_path)
            w.require_code(
                not process_identity(marker.get("pid", 0)),
                "PLATFORM_CLIENT_UNTRACKED",
                f"AFX client already running without matching launch evidence; see {marker_path}",
            )
        # Official one-shot command checks authentication before launching the resident process.
        try:
            w.capture(base + ["once", *options], env=env, cwd=home, timeout=90)
        except Exception as error:
            raise w.WorkflowError(
                "PLATFORM_CLIENT_START_FAILED",
                "AFX client preflight failed; check endpoint and client token permissions",
            ) from error
        with (home / "client.log").open("ab") as log:
            process = subprocess.Popen(
                argv,
                env=env,
                cwd=home,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        start_time = process_identity(process.pid)
        w.require_code(
            start_time is not None,
            "PLATFORM_CLIENT_START_FAILED",
            f"AFX exited during startup; see {home / 'client.log'}",
        )
        w.write(
            record_path,
            {"pid": process.pid, "start_time": start_time, "expected": expected},
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            w.require_code(
                process.poll() is None,
                "PLATFORM_CLIENT_START_FAILED",
                f"AFX exited during startup; see {home / 'client.log'}",
            )
            if marker_path.exists() and w.read(marker_path).get("pid") == process.pid:
                return {
                    "pid": process.pid,
                    "reused": False,
                    "log": str(home / "client.log"),
                }
            time.sleep(0.2)
        raise w.WorkflowError(
            "PLATFORM_CLIENT_START_FAILED",
            f"AFX startup not confirmed; see {home / 'client.log'}",
        )
