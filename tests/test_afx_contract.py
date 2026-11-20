"""Opt-in contracts against an AFX checkout, with disposable local databases."""

from afxx import common as workflow_common

import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_platform_adapter import PACKAGE
from afxx.afx import replay as d
from afxx.afx import platform as p


@pytest.fixture
def afx(monkeypatch, request):
    source = os.environ.get("AFX_SOURCE")
    if not source:
        if request.config.getoption("--require-afx-contracts"):
            pytest.fail("release validation requires AFX_SOURCE pointing to an AFX checkout")
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


def test_numbered_issue_workflow(afx, platform_cfg, candidate, monkeypatch, tmp_path):
    """Exercise the CLI through the v3 service with a temporary platform database."""
    import json
    import afxx.cli
    from afxx import jobs
    from autofusex.platform import auth, issues, regression, retests, queries
    from autofusex.platform.ingest import Ingestor
    from autofusex.platform.store import Store

    store = Store(tmp_path / "numbered-platform")
    auth.add_user(store, "dev", "developer")

    def principal():
        return auth.authenticate(
            store, "Bearer " + auth.issue_token(store, "dev")
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
                "log": "schedule failed: test execution log",
                "created_at": 1000,
                "e2e_ns": 10,
            }
        ],
    }
    result = Ingestor(store).upload(
        principal(), upload, json.dumps(upload).encode()
    )
    assert result["results"][0]["status"] == "accepted", result["results"]
    number = issues.list_issues(store)["issues"][0]["issue_id"]
    agent = principal()
    verify_keys = []
    summary_keys = []

    def api(c, method, path, body=None, **opts):
        if method == "GET":
            if path == f"/api/issues/{number}":
                return issues.issue_detail(store, number)
            if path == f"/api/issues/{number}/rounds/1/tasks":
                return retests.round_tasks(store, number, 1)
            if path == f"/api/regression/preview?issue_id={number}":
                return regression.preview(store, number)
            assert path == "/api/executions/exec-0001"
            return queries.execution_detail(store, "exec-0001")
        assert method == "POST"
        if path == "/api/issues/claim":
            assert body["issue_id"] == number
            return issues.claim(store, agent, dict(body))
        if path == f"/api/issues/{number}/renew":
            return issues.renew(store, agent, number, dict(body))
        if path == f"/api/issues/{number}/knowledge":
            assert not opts.get("remote")
            summary_keys.append(body["request_key"])
            value = issues.submit_knowledge(store, agent, number, dict(body))
            if len(summary_keys) == 1:
                raise workflow_common.WorkflowError("PLATFORM_UNAVAILABLE", "summary response lost")
            return value
        assert path == f"/api/issues/{number}/verify" and opts["remote"]
        verify_keys.append(body["request_key"])
        value = issues.verify(store, agent, number, dict(body))
        if len(verify_keys) == 1:
            raise workflow_common.WorkflowError("PLATFORM_UNAVAILABLE", "response lost")
        return value

    monkeypatch.setattr(p, "call", api)
    monkeypatch.setattr(afxx.cli.workflow_config, "config", lambda _: platform_cfg)

    def cli(command, *args):
        monkeypatch.setattr(
            sys, "argv", ["afxx", *command.split(), "--issue-id", number, *args]
        )
        return afxx.cli.main()

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

    pending = cli("issue status", "--round-no", "1")
    assert pending["terminal"] is False and pending["verification"]["kind"] == "verify"

    assert cli("issue detail")["issue_id"] == number
    tasks = cli("issue tasks", "--round-no", "1")
    assert len(tasks["tasks"]) == 1 and tasks["tasks"][0]["case_key"] == "E" + "0" * 25 + "1"
    preview = cli("issue regression-preview")
    assert preview["issue_id"] == number and "cases" in preview
    monkeypatch.setattr(sys, "argv", ["afxx", "execution", "show", "--exec-uuid", "exec-0001"])
    execution = afxx.cli.main()
    assert execution["log"] == "schedule failed: test execution log"
    assert execution["log_truncated"] is False

    # Complete this fixture's verification without regression candidates, then publish via the real service.
    with store.write() as conn:
        conn.execute("UPDATE retest_tasks SET status='done', result='pass' WHERE issue_id=?", (number,))
        retests._maybe_finish(conn, number, 1)
    assert issues.issue_detail(store, number)["status"] == "summarizing"
    summary = tmp_path / "summary.yaml"
    summary.write_text("kind: bugfix_function\ntitle: Fix scheduling\nsymptom: Compile failed\nroot_cause: Incorrect boundary\nsolution: Correct the boundary\nfix_ref: abc123\n")
    with pytest.raises(workflow_common.WorkflowError, match="summary response lost"):
        cli("issue summarize", "--summary-file", str(summary), "--request-id", "summary-1")
    assert issues.issue_detail(store, number)["status"] == "resolved"
    published = cli("issue summarize", "--summary-file", str(summary), "--request-id", "summary-1")
    assert published["status"] == "resolved" and published["knowledge_id"].startswith("K")
    assert cli("issue summarize", "--summary-file", str(summary), "--request-id", "summary-1") == published
    assert summary_keys == ["summary-1", "summary-1"]
