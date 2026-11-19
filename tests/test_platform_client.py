# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
from af_forge import common as workflow_common
from af_forge.environment import runtime as workflow_runtime

import contextlib
import signal
import sys
from types import SimpleNamespace

import pytest
from af_forge.afx import client as pc


def test_stop_never_signals_reused_pid(platform_cfg, monkeypatch):
    workflow_common.write(
        pc.directory(platform_cfg) / "process.json",
        {"pid": 123, "start_time": "old", "state": "ready"},
    )
    monkeypatch.setattr(pc, "process_identity", lambda pid: "new")
    monkeypatch.setattr(
        pc.os, "kill", lambda *args: pytest.fail("must not kill reused PID")
    )
    assert pc.stop(platform_cfg)["state"] == "failed"


def test_deployment_drains_before_work(platform_cfg, monkeypatch):
    events = []
    monkeypatch.setattr(pc, "stop", lambda c: events.append("stop"))
    with pc.deployment(platform_cfg):
        events.append("deploy")
    assert events == ["stop", "deploy"]


def test_busy_client_stop_blocks_deploy(platform_cfg, monkeypatch):
    def busy(c):
        raise ValueError("client is draining an active replay")

    monkeypatch.setattr(pc, "stop", busy)
    with pytest.raises(ValueError, match="draining"):
        with pc.deployment(platform_cfg):
            pytest.fail("must not deploy over active replay")


def test_start_uses_selected_python_cann_and_persistent_code(
    platform_cfg, tmp_path, monkeypatch
):
    root = pc.directory(platform_cfg)
    calls = []
    monkeypatch.setattr(
        workflow_runtime,
        "environment",
        lambda c: {"ASCEND_HOME_PATH": c["runtime"]["install_root"] + "/cann"},
    )
    states = iter([{"state": "stopped"}, {"state": "ready", "pid": 42}])
    monkeypatch.setattr(pc, "status", lambda c: next(states))
    monkeypatch.setattr(pc, "process_identity", lambda pid: None)

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(pid=42, poll=lambda: None)

    monkeypatch.setattr(pc.subprocess, "Popen", spawn)
    assert pc.start(platform_cfg)["state"] == "ready"
    argv, options = calls[0]
    assert argv == [
        workflow_runtime.python(platform_cfg),
        "-m",
        "af_forge.afx.client",
        "_run",
        str(root),
    ]
    assert (
        options["env"]["ASCEND_HOME_PATH"]
        == platform_cfg["runtime"]["install_root"] + "/cann"
    )
    assert options["env"]["AUTOFUSEX_HOME"] == str(root / "home")
    assert options["start_new_session"] is True
    assert "secret" not in (root / "config.json").read_text()
    assert (root / "af_forge/cli.py").is_file()


def test_start_reuses_only_matching_ready_client(platform_cfg, monkeypatch):
    monkeypatch.setattr(
        pc, "status", lambda c: {"state": "ready", "config_sha256": pc.fingerprint(c)}
    )
    monkeypatch.setattr(
        pc.subprocess, "Popen", lambda *a, **k: pytest.fail("duplicate client")
    )
    assert pc.start(platform_cfg)["state"] == "ready"
    monkeypatch.setattr(
        pc, "status", lambda c: {"state": "ready", "config_sha256": "old"}
    )
    with pytest.raises(ValueError, match="configuration changed"):
        pc.start(platform_cfg)


@pytest.mark.parametrize("upload_retry", [False, True])
def test_runner_uses_current_consumer_and_drains_under_locks(
    platform_cfg, monkeypatch, upload_retry
):
    import types

    root = pc.directory(platform_cfg)
    workflow_common.write(root / "config.json", platform_cfg)
    monkeypatch.setattr(sys, "prefix", platform_cfg["runtime"]["venv_dir"])
    monkeypatch.setenv(
        "ASCEND_HOME_PATH", platform_cfg["runtime"]["install_root"] + "/cann"
    )
    monkeypatch.setattr(pc, "process_identity", lambda pid: "start")
    monkeypatch.setattr(pc.time, "sleep", lambda seconds: None)
    signals = {}
    monkeypatch.setattr(
        pc.signal, "signal", lambda sig, handler: signals.update({sig: handler})
    )
    active = []

    @contextlib.contextmanager
    def lock(path, shared=False):
        assert shared
        active.append(path)
        try:
            yield
        finally:
            active.remove(path)

    monkeypatch.setattr(workflow_common, "lock", lock)
    calls, consumers, uploads = [], [], []

    class Consumer:
        def __init__(self, home, database, devices):
            self.home, self.database, self.devices = home, database, devices
            self.ledger = object()
            self.ticks = 0
            consumers.append(self)

        def api(self, route, payload):
            calls.append(route)
            return {"items": []}

        def tick(self):
            assert len(active) == 2
            self.ticks += 1
            self.api("register", {})
            self.api("claim", {})
            if self.ticks == 2:
                signals[signal.SIGTERM]()
                return {"inflight": 1}
            return {"inflight": 0}

    def upload(home, database):
        assert len(active) == 2
        uploads.append(True)
        if upload_retry and len(uploads) == 2:
            raise OSError("transient upload failure during drain")
        return {"pending": 0}

    class Budget:
        def __init__(self, **kwargs):
            self.options = kwargs

        def admit(self, home):
            pass

    def save_budget(ledger, budget):
        assert budget.options == {"compile_threads": 2, "cpu_workers": 2}

    modules = {
        "autofusex.fuzz.client.environment": dict(
            collect=lambda: {"cann": "9.3", "autofuse_binary": "a" * 64}
        ),
        "autofusex.fuzz.client.scheduling.devices": dict(
            select_devices=lambda requested: ["npu:0"]
        ),
        "autofusex.fuzz.client.scheduling.pool": dict(
            discover=lambda devices: [SimpleNamespace(model="fixture-npu")]
        ),
        "autofusex.fuzz.client.scheduling.budgets": dict(
            Budget=Budget, write_policy=save_budget
        ),
        "autofusex.fuzz.client.scheduling.ownership": dict(
            own_queue=lambda ledger: contextlib.nullcontext()
        ),
        "autofusex.fuzz.client.scheduling.hosting": dict(
            status=lambda ledger: {"ownership": "unowned"}
        ),
        "autofusex.fuzz.client.sync.client": dict(
            configure=lambda *a: None,
            verify_identity=lambda home: {"user_id": "user"},
            once=upload,
        ),
        "autofusex.fuzz.client.sync.retests": dict(Consumer=Consumer),
        "autofusex.fuzz.client.sync.outbox": dict(
            pending=lambda *a, **k: {
                "source_id": "33333333-3333-4333-8333-333333333333"
            }
        ),
    }
    for name, attrs in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    assert pc.run(root) is None
    assert not active
    assert calls.count("claim") == 1  # no startup claims; no claims during drain
    assert consumers[0].ticks == 3
    assert len(uploads) == (3 if upload_retry else 2)
    state = workflow_common.read(root / "process.json")
    assert state["state"] == "stopped"
    assert state["client_id"] == "33333333-3333-4333-8333-333333333333"
    assert state["installed"]["af-mlir"]["sha256"] == "a" * 64
