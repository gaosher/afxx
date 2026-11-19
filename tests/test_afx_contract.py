# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Opt-in contracts against an AFX checkout, with disposable local databases."""

from af_forge import common as workflow_common

import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_platform_adapter import PACKAGE
from af_forge.afx import replay as d
from af_forge.afx import platform as p


@pytest.fixture
def afx(monkeypatch):
    source = os.environ.get("AFX_SOURCE")
    if not source:
        pytest.skip("set AFX_SOURCE to test against the current AFX implementation")
    source = str(Path(source).resolve())
    assert Path(source, "autofusex").is_dir()
    monkeypatch.syspath_prepend(source)
    monkeypatch.setenv(
        "PYTHONPATH", source + os.pathsep + os.environ.get("PYTHONPATH", "")
    )


@pytest.mark.parametrize("count", range(1, 9))
def test_real_seed_and_saved_result_cli(afx, tmp_path, count):
    from autofusex.fuzz.client.generation.sampling import SamplingConfig
    from autofusex.fuzz.client.storage import ResultRepository

    seed = SamplingConfig(dynamic_variants=max(2, count)).seed(42, dynamic=count != 1)

    def capture(argv):
        return subprocess.check_output(argv, text=True)

    normalized = d.normalized_seeds(
        capture(d.seed_command(sys.executable, [seed.short]))
    )
    assert normalized == [[str(seed), count]]
    repo = ResultRepository(tmp_path / "results.sqlite")
    for backend in ("cpu", "af-mlir"):
        repo.save(
            dict(
                seed=str(seed),
                workload_id="a" * 64,
                oracle_id="cpu/1",
                backend=backend,
                status="ok",
                shape_results=[
                    dict(
                        shape_index=i,
                        correctness="not_checked" if backend == "cpu" else "pass",
                        performance="not_requested",
                    )
                    for i in range(count)
                ],
            )
        )
    attempts = d.read_results(sys.executable, repo.path, capture)
    assert d.summarize(attempts, normalized)["passed"]


def test_real_repairs_submission_retry_and_accepted_result(
    afx, platform_cfg, candidate, monkeypatch, tmp_path
):
    from contextlib import closing
    from autofusex.fuzz.client.generation.sampling import SamplingConfig
    from autofusex.fuzz.client.storage import ResultRepository
    from autofusex.fuzz.client.storage.repository import _connect
    from autofusex.fuzz.client.sync.outbox import pending
    from autofusex.fuzz.server.central import repairs, retests, sessions
    from autofusex.fuzz.server.central.repository import create_user, ingest

    database = tmp_path / "central.sqlite"
    user = create_user(database, "Workflow contract")
    with closing(_connect(database, writable=True)) as db, db:
        sessions.initialize(db)
        retests.initialize(db)
    original = ResultRepository(tmp_path / "original.sqlite")
    seed = SamplingConfig(dynamic_variants=2).seed(42, dynamic=True)
    original.save(
        dict(
            seed=str(seed),
            workload_id="a" * 64,
            oracle_id="cpu/1",
            backend="af-mlir",
            status="fail",
            execution=dict(backend="af-mlir", static_only=False),
            evaluation=dict(
                correctness=True, performance=False, warmup_steps=1, measured_steps=1
            ),
            shape_results=[
                dict(shape_index=i, correctness="fail", performance="not_requested")
                for i in range(2)
            ],
        )
    )
    ingest(database, user["token"], pending(original.path))
    fixed = ResultRepository(tmp_path / "fixed.sqlite")
    client_id = pending(fixed.path)["source_id"]
    retests.register(
        database,
        user["user_id"],
        dict(
            client_id=client_id,
            machine_id="workflow-contract",
            name="isolated",
            backends=["af-mlir"],
        ),
    )
    monkeypatch.setattr(
        "af_forge.afx.client.status",
        lambda c: dict(
            state="ready", client_id=client_id, installed={"af-mlir": PACKAGE}
        ),
    )
    submissions = []

    def api(c, method, path, body=None, **kwargs):
        assert path.startswith(p.API_ROOT)
        parts = path[len(p.API_ROOT) :].strip("/").split("/")
        if method == "GET":
            return (
                retests.clients(database, user["user_id"])
                if parts == ["clients"]
                else repairs.get(database, user["user_id"], parts[0])
            )
        action = parts[-1]
        response = repairs.mutate(
            database,
            user["user_id"],
            action,
            body,
            None if action == "claim" else parts[0],
        )
        if action == "retest":
            submissions.append(body.copy())
            if len(submissions) == 1:
                raise workflow_common.WorkflowError(
                    "PLATFORM_UNAVAILABLE", "lost response after server committed"
                )
        return response

    monkeypatch.setattr(p, "call", api)
    claimed = p.claim(platform_cfg, None, "claim-contract")
    repair_id = claimed["id"]
    assert len(p.preview(platform_cfg, repair_id)["snapshot"]["targets"]) == 2
    p.renew(platform_cfg, claimed)
    workflow_common.write(
        candidate / "request.json",
        {"issue": {"id": repair_id, "lease_id": claimed["lease_id"]}},
    )
    with pytest.raises(workflow_common.WorkflowError):
        p.verify(platform_cfg, candidate)
    submission = p.verify(platform_cfg, candidate)
    assert submissions[0] == submissions[1]
    assert not p.poll(platform_cfg, repair_id, submission["retest_id"])["terminal"]
    task = retests.claim(database, user["user_id"], dict(client_id=client_id, limit=1))[
        "items"
    ][0]
    spec = task["spec"]
    attempt = fixed.save(
        dict(
            seed=spec["seed"],
            workload_id=spec["workload_id"],
            oracle_id=spec["oracle_id"],
            backend="af-mlir",
            status="ok",
            evaluation=spec["evaluation"],
            execution=dict(
                backend="af-mlir", static_only=False, batch_id=task["lease_id"]
            ),
            shape_results=[
                dict(shape_index=i, correctness="pass", performance="not_requested")
                for i in range(2)
            ],
        )
    )
    ingest(database, user["token"], pending(fixed.path))
    assert retests.complete(
        database,
        user["user_id"],
        dict(client_id=client_id, lease_id=task["lease_id"], attempt_id=attempt),
    )["accepted"]
    result = p.poll(platform_cfg, repair_id, submission["retest_id"])
    assert result["terminal"] and result["outcome"] == "passed"
    assert result["status"] == "verified"


def test_numbered_issue_workflow(afx, platform_cfg, candidate, monkeypatch, tmp_path):
    """Exercise the CLI through the v3 service with a temporary platform database."""
    import json
    import af_forge.cli
    from af_forge import jobs
    from autofusex.platform import auth, issues
    from autofusex.platform.ingest import Ingestor
    from autofusex.platform.store import Store

    store = Store(tmp_path / "numbered-platform")
    auth.add_user(store, "dev", "developer")

    def principal(kind):
        return auth.authenticate(
            store, "Bearer " + auth.issue_token(store, "dev", kind)
        )

    upload = {
        "request_key": "seed-issue",
        "client": {"machine_id": "npu", "npu_arch": "910b"},
        "run": {"run_uuid": "run-0001", "kind": "fuzz"},
        "items": [
            {
                "exec_uuid": "exec-0001",
                "case": {
                    "case_key": "E" + "0" * 25 + "1",
                    "case_source": "nnsmith",
                    "is_dynamic": False,
                    "shape_count": 1,
                    "node_count": 6,
                    "op_types": ["add"],
                    "dtypes": ["float16"],
                    "max_numel": 4096,
                },
                "backend": "af-mlir",
                "npu_arch": "910b",
                "env": {"cann": "9.3"},
                "function_status": "compile_fail",
                "fail_stage": "schedule",
                "log_summary": "schedule failed",
                "created_at": 1000,
                "e2e_ns": 10,
            }
        ],
    }
    result = Ingestor(store).upload(
        principal("client"), upload, json.dumps(upload).encode()
    )
    assert result["results"][0]["status"] == "accepted", result["results"]
    number = issues.list_issues(store)["issues"][0]["issue_id"]
    agent = principal("agent")
    verify_keys = []

    def api(c, method, path, body=None, **opts):
        assert method == "POST"
        if path == "/api/issues/claim":
            assert body["issue_id"] == number
            return issues.claim(store, agent, dict(body))
        if path == f"/api/issues/{number}/renew":
            return issues.renew(store, agent, number, dict(body))
        assert path == f"/api/issues/{number}/verify" and opts["remote"]
        verify_keys.append(body["request_key"])
        value = issues.verify(store, agent, number, dict(body))
        if len(verify_keys) == 1:
            raise workflow_common.WorkflowError("PLATFORM_UNAVAILABLE", "response lost")
        return value

    monkeypatch.setattr(p, "call", api)
    monkeypatch.setattr(af_forge.cli.workflow_config, "config", lambda _: platform_cfg)

    def cli(command, *args):
        monkeypatch.setattr(
            sys, "argv", ["af-forge", *command.split(), "--issue-id", number, *args]
        )
        return af_forge.cli.main()

    first = cli("issue claim")
    assert (
        cli("issue claim", "--request-id", first["request_id"])["lease_id"]
        == first["lease_id"]
    )
    assert cli("issue preview")["seeds"] == ["E" + "0" * 25 + "1"]
    assert (
        cli("issue preview", "--full")["snapshot"]["cases"][0]["exec_uuid"]
        == "exec-0001"
    )
    assert cli("issue renew")["issue_id"] == number
    monkeypatch.setattr(jobs, "local_preflight", lambda *args: None)
    monkeypatch.setattr(jobs, "submit", lambda c, request, *args: request)
    request = cli("deploy")
    assert request["issue"] == {"id": number, "lease_id": first["lease_id"]}
    workflow_common.write(candidate / "request.json", request)

    def remote(c, argv):
        value = p.verify(c, candidate, json.loads(argv[-1]))
        return {"state": "success", "verification": value}

    monkeypatch.setattr(jobs, "remote", remote)
    with pytest.raises(workflow_common.WorkflowError, match="response lost"):
        cli("issue verify", "--job-id", "a" * 32)
    submitted = cli("issue verify", "--job-id", "a" * 32)
    assert submitted["round_no"] == 1 and submitted["response"]["tasks"] == 1
    assert cli("issue verify", "--job-id", "a" * 32) == submitted
    assert len(verify_keys) == 2 and verify_keys[0] == verify_keys[1]
