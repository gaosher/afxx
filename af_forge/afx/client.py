# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Own a dedicated AFX retest process for one workflow environment."""

from af_forge import bundle as workflow_bundle
from af_forge import common as workflow_common
from af_forge.environment import runtime as workflow_runtime

import contextlib
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


def directory(c):
    return Path(c["runtime"]["venv_dir"]).parent / (
        "platform-client-" + workflow_common.digest(c["runtime"]["venv_dir"])[:12]
    )


def fingerprint(c):
    return workflow_common.digest(
        {
            "protocol": "repairs-v1",
            **{key: c[key] for key in ("runtime", "platform", "replay")},
        }
    )


def process_identity(pid):
    try:
        stat = Path("/proc", str(pid), "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return stat[19]
    except (OSError, IndexError):
        return None


def status(c):
    root = directory(c)
    path = root / "process.json"
    if not path.exists():
        return {"state": "stopped", "directory": str(root)}
    value = workflow_common.read(path)
    live = process_identity(value.get("pid"))
    if not live or live != value.get("start_time"):
        value = dict(
            value, state="stopped" if value["state"] == "stopped" else "failed"
        )
    return dict(value, directory=str(root), log=str(root / "client.log"))


def stop(c, timeout=60):
    """Called with control.lock. Drain current replay, never kill its subprocesses."""
    root = directory(c)
    value = status(c)
    if value["state"] in ("stopped", "failed") and not process_identity(
        value.get("pid")
    ):
        return value
    pid = value.get("pid")
    if not pid or process_identity(pid) != value.get("start_time"):
        workflow_common.require(
            value["state"] != "starting",
            "client startup is uncertain; inspect client.log",
        )
        return value
    command = Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")
    workflow_common.require(
        str(root).encode() in command and b"_run" in command,
        "recorded PID is not the managed retest client",
    )
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while process_identity(pid) == value["start_time"]:
        workflow_common.require(
            time.monotonic() < deadline,
            "client is draining an active replay; retry after it finishes",
        )
        time.sleep(0.2)
    return status(c)


def start(c, timeout=90):
    """Called with control.lock; ready handshake happens before any task is claimed."""
    root = directory(c)
    value = status(c)
    if value["state"] == "ready":
        workflow_common.require(
            value.get("config_sha256") == fingerprint(c),
            "client configuration changed; stop then start it",
        )
        return value
    if process_identity(value.get("pid")) == value.get("start_time") and value.get(
        "pid"
    ):
        raise ValueError(
            "managed client is starting, stopping or unhealthy; stop it before restarting"
        )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    workflow_bundle.copy_to(root)
    workflow_common.write(root / "config.json", c)
    env = workflow_runtime.environment(c)
    env.update(AUTOFUSEX_HOME=str(root / "home"))
    (root / "home").mkdir(exist_ok=True)
    with (root / "client.log").open("ab") as log:
        child = subprocess.Popen(
            [
                workflow_runtime.python(c),
                "-m",
                "af_forge.afx.client",
                "_run",
                str(root),
            ],
            env=env,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    deadline = time.monotonic() + timeout
    while child.poll() is None:
        value = status(c)
        if value.get("pid") == child.pid and value["state"] == "ready":
            return value
        if value.get("pid") == child.pid and value["state"] == "failed":
            break
        if time.monotonic() >= deadline:
            child.terminate()
            raise ValueError(
                "client startup timed out; inspect " + str(root / "client.log")
            )
        time.sleep(0.2)
    raise ValueError("client startup failed; inspect " + str(root / "client.log"))


@contextlib.contextmanager
def deployment(c):
    root = directory(c)
    with workflow_common.lock(root / "control.lock"):
        stop(c)
        yield


def manage(c, action):
    with workflow_common.lock(directory(c) / "control.lock"):
        if action == "status":
            return status(c)
        if action == "stop":
            return stop(c)
        with (
            workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
            workflow_common.lock(
                workflow_runtime.registry(c).with_suffix(".lock"), shared=True
            ),
        ):
            return start(c)


def run(root):
    root = Path(root)
    c = workflow_common.read(root / "config.json")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *args: stopped.set())
    signal.signal(signal.SIGINT, lambda *args: stopped.set())
    record = {
        "pid": os.getpid(),
        "start_time": process_identity(os.getpid()),
        "state": "starting",
        "config_sha256": fingerprint(c),
    }

    def publish(state, **fields):
        record.update(state=state, updated_at=time.time(), **fields)
        workflow_common.write(root / "process.json", record)

    with (root / "instance.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        publish("starting")
        try:
            workflow_common.require(
                Path(sys.prefix).resolve() == Path(c["runtime"]["venv_dir"]).resolve(),
                "client is outside the configured venv",
            )
            workflow_common.require(
                Path(os.environ.get("ASCEND_HOME_PATH", "")).resolve()
                == (Path(c["runtime"]["install_root"]) / "cann").resolve(),
                "client CANN environment mismatch",
            )
            from autofusex.fuzz.client.environment import collect
            from autofusex.fuzz.client.scheduling.devices import select_devices
            from autofusex.fuzz.client.scheduling.pool import discover
            from autofusex.fuzz.client.scheduling.budgets import Budget, write_policy
            from autofusex.fuzz.client.scheduling.hosting import status as queue_status
            from autofusex.fuzz.client.sync.client import (
                configure,
                verify_identity,
                once,
            )
            from autofusex.fuzz.client.sync.retests import Consumer
            from autofusex.fuzz.client.sync.outbox import pending

            home, database = root / "home", root / "retest-results.sqlite"
            home.mkdir(exist_ok=True)
            configure(home, c["platform"]["url"], c["platform"]["remote_token_file"])
            publish("starting", phase="credentials")
            principal = verify_identity(home)
            publish("starting", phase="devices")
            requested = c["replay"]["devices"]
            devices = select_devices(None if requested == "auto" else requested)
            workflow_common.require(
                len({d.model for d in discover(devices)}) == 1,
                "select replay.devices with a single NPU architecture",
            )

            class ManagedConsumer(Consumer):
                accepting = False

                def api(self, route, payload):
                    # Drain accepted tasks after TERM; never acquire another lease.
                    if route == "claim" and (not self.accepting or stopped.is_set()):
                        return {"items": []}
                    return super().api(route, payload)

            consumer = ManagedConsumer(home, database, devices=devices)
            workers = (
                len(devices)
                if c["replay"]["workers"] == "auto"
                else c["replay"]["workers"]
            )
            budget = Budget(
                compile_threads=c["replay"]["compile_threads"], cpu_workers=workers
            )
            budget.admit(home)
            from autofusex.fuzz.client.scheduling.ownership import own_queue

            with own_queue(consumer.ledger):
                write_policy(consumer.ledger, budget)
            # AFX schedules workers asynchronously. Hold these locks for the entire
            # consumer lifetime, including upload/ACK and supervisor shutdown.
            with (
                workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
                workflow_common.lock(
                    workflow_runtime.registry(c).with_suffix(".lock"), shared=True
                ),
            ):
                publish("starting", phase="environment")
                package = workflow_runtime.af_identity(collect())
                consumer.tick()  # register/recover, with new claims disabled
                client_id = pending(database, limit=1)["source_id"]
                publish(
                    "ready",
                    installed={"af-mlir": package},
                    client_id=client_id,
                    devices=devices,
                    python=sys.executable,
                    cann_home=os.environ.get("ASCEND_HOME_PATH"),
                    user_id=principal["user_id"],
                )
                consumer.accepting = True
                while True:
                    try:
                        workflow_common.require(
                            workflow_runtime.af_identity(collect()) == package,
                            "AF changed; inspect the managed environment before continuing",
                        )
                        # AFX owns serialization, upload receipts and lease completion.
                        for _ in range(20):
                            uploaded = once(home, database)
                            if not (
                                uploaded.get("uploaded")
                                or uploaded.get("fact_uploaded")
                            ):
                                break
                        progress = consumer.tick()
                        if (
                            stopped.is_set()
                            and progress.get("inflight") == 0
                            and queue_status(consumer.ledger).get("ownership")
                            == "unowned"
                        ):
                            break
                        publish("draining" if stopped.is_set() else "ready", error=None)
                    except Exception as error:
                        publish("unhealthy", error=type(error).__name__)
                        print("AFX cycle failed: " + type(error).__name__, flush=True)
                    if stopped.is_set():
                        time.sleep(1)
                    else:
                        stopped.wait(10)
            publish("stopped")
        except BaseException as error:
            publish("failed", error=type(error).__name__)
            print(
                "AFX client failed: "
                + type(error).__name__
                + "; check environment and credentials",
                flush=True,
            )
            return 1


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(run(sys.argv[2]))
