import json
import sys
import pytest
from af_forge import cli, config, jobs, common
from af_forge.afx import platform as p


CASES = [
    (["issue", "detail", "--issue-id", "F152"], "/api/issues/F152", {"issue_id": "F152", "rounds": [], "events": []}),
    (["issue", "tasks", "--issue-id", "F152", "--round-no", "2"], "/api/issues/F152/rounds/2/tasks", [{"task_id": 1, "result": "fail", "detail": "compile error"}]),
    (["issue", "regression-preview", "--issue-id", "F152"], "/api/regression/preview?issue_id=F152", {"issue_id": "F152", "cases": [], "budget": 200}),
    (["execution", "show", "--exec-uuid", "exec-0001"], "/api/executions/exec-0001", {"exec_uuid": "exec-0001", "log": "failure", "log_truncated": True}),
]


@pytest.mark.parametrize("args,path,data", CASES)
def test_queries_are_local_read_only_without_claim(platform_cfg, monkeypatch, capsys, args, path, data):
    monkeypatch.setattr(sys, "argv", ["af-forge", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("must not use NPU"))
    monkeypatch.setattr(p, "claimed", lambda *a: pytest.fail("must not require lease"))
    def api(c, method, target, body=None, **kw):
        assert method == "GET" and target == path and body is None
        assert not kw.get("remote")
        return data
    monkeypatch.setattr(p, "call", api)
    assert cli.cli() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == " ".join(args[:2])
    assert result["data"] == (dict(issue_id="F152", round_no=2, tasks=data) if args[1] == "tasks" else data)


@pytest.mark.parametrize("args", [
    ["issue", "detail", "--issue-id", "../F152"],
    ["issue", "tasks", "--issue-id", "F152", "--round-no", "0"],
    ["issue", "tasks", "--issue-id", "F152", "--round-no", "1000000"],
    ["issue", "tasks", "--issue-id", "F152"],
    ["execution", "show", "--exec-uuid", "../exec-0001"],
    ["execution", "show", "--exec-uuid", "exec-0001?secret"],
    ["execution", "show", "--exec-uuid", "123"],
])
def test_invalid_query_never_reaches_platform(platform_cfg, monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["af-forge", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(p, "call", lambda *a, **k: pytest.fail("invalid request"))
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize("args,path,data", CASES)
def test_query_platform_errors_propagate(platform_cfg, monkeypatch, capsys, args, path, data):
    monkeypatch.setattr(sys, "argv", ["af-forge", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    def fail(*a, **kw):
        raise common.WorkflowError("PLATFORM_NOT_FOUND", "not found")
    monkeypatch.setattr(p, "call", fail)
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PLATFORM_NOT_FOUND"


def test_tasks_empty_and_execution_missing_log_preserved(platform_cfg, monkeypatch):
    monkeypatch.setattr(p, "call", lambda *a, **k: [])
    assert p.issue_tasks(platform_cfg, "F152", 1)["tasks"] == []
    value = {"exec_uuid": "exec-0001", "log_missing": True, "shapes": []}
    monkeypatch.setattr(p, "call", lambda *a, **k: value)
    assert p.execution_detail(platform_cfg, "exec-0001") == value


@pytest.mark.parametrize("query,args,value", [
    (p.issue_detail, ("F152",), {"issue_id": "F153"}),
    (p.issue_tasks, ("F152", 1), {}),
    (p.regression_preview, ("F152",), {"issue_id": "F153"}),
    (p.execution_detail, ("exec-0001",), {"exec_uuid": "exec-0002"}),
])
def test_query_rejects_malformed_response(platform_cfg, monkeypatch, query, args, value):
    monkeypatch.setattr(p, "call", lambda *a, **k: value)
    with pytest.raises(common.WorkflowError) as error:
        query(platform_cfg, *args)
    assert error.value.code == "PLATFORM_RESPONSE_INVALID"
