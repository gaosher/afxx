from concurrent.futures import ThreadPoolExecutor
import sys
import pytest
from afxx import cli, common, jobs, deploy_runs, replay_runs, run_groups


def test_deploy_rounds_are_isolated_from_replay(cfg, monkeypatch):
    submissions = []
    def submit(c, request, execution, dry):
        submissions.append((execution, request))
        return {"job": execution, "state": "queued"}
    monkeypatch.setattr(jobs, "submit", submit)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda n: deploy_runs.submit(cfg, {"command": "deploy", "source": n}, "repair"), range(8)))
    assert sorted(row["run"] for row in values) == list(range(1, 9))
    assert len({execution for execution, _ in submissions}) == 8
    assert {request["source"] for _, request in submissions} == set(range(8))
    assert replay_runs.submit(cfg, {"seeds": ["ABC"]}, "repair")["run"] == 1
    assert len(run_groups.load(cfg, "repair", "deploy")["runs"]) == 8
    cfg["connection"]["environment"] = "other"
    with pytest.raises(common.WorkflowError, match="state_dir"):
        deploy_runs.list_runs(cfg, "repair")


def test_uncertain_submission_keeps_round_and_can_query(cfg, monkeypatch):
    def fail(*args):
        raise common.WorkflowError("REMOTE_UNAVAILABLE", "lost")
    monkeypatch.setattr(jobs, "submit", fail)
    with pytest.raises(common.WorkflowError) as error:
        deploy_runs.submit(cfg, {"command": "deploy"}, "repair")
    assert error.value.data == {"request_id": "repair", "run": 1, "state": "submission_unknown"}
    monkeypatch.setattr(jobs, "query_job", lambda *a: {"state": "success", "result.json": {"runtime": {"hash": "A"}}})
    shown = deploy_runs.show(cfg, "repair", 1)
    assert shown["result"]["runtime"]["hash"] == "A"
    assert shown["state"] == "success"
    assert "execution" not in shown


@pytest.mark.parametrize("action", ["list", "show"])
def test_deploy_query_routes(cfg, monkeypatch, action):
    monkeypatch.setattr(cli.workflow_config, "config", lambda *a: cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(jobs, "submit", lambda *a: {"state": "prepared"})
    deploy_runs.submit(cfg, {"command": "deploy"}, "repair", True)
    monkeypatch.setattr(sys, "argv", ["afxx", "deploy", action, "--request-id", "repair"])
    assert cli.main()["request_id"] == "repair"
    monkeypatch.setattr(sys, "argv", ["afxx", "deploy", "--coordinate-client", action, "--request-id", "repair"])
    with pytest.raises(common.WorkflowError, match="不接受执行参数"):
        cli.parse_arguments()


def test_public_receipt_keeps_group_and_run():
    result = cli.response("deploy", {"request_id": "repair", "run": 2, "job": "internal", "status": {"job": "internal", "state": "success"}})
    assert result["data"] == {"request_id": "repair", "run": 2, "state": "success", "available_details": []}


def test_mcp_deploy_allocates_invocations_for_same_group(monkeypatch):
    from afxx import mcp_server as m, mcp_operations as op
    from test_mcp_tools import call
    calls = []
    def submit(command, args, path, key):
        calls.append((args, key))
        return {"ok": True, "data": {"request_id": key}}
    monkeypatch.setattr(op, "submit", submit)
    server = m.create_server("/config")
    for _ in range(2):
        result = call(server, "afxx_deploy", {"request_id": "repair"})
        assert result.structuredContent["data"]["group_request_id"] == "repair"
    assert calls[0][1] != calls[1][1]
    assert all(args[-2:] == ["--request-id", "repair"] for args, _ in calls)


def test_mcp_verify_current_group_and_conflicting_id(monkeypatch):
    from afxx import mcp_server as m, mcp_operations as op
    from test_mcp_tools import call
    seen = []
    monkeypatch.setattr(op, "submit", lambda *args: seen.append(args) or {"ok": True, "data": {}})
    server = m.create_server("/config")
    result = call(server, "afxx_issue", {"action": "verify", "issue_id": "F152", "request_id": "repair"})
    assert not result.isError
    assert seen[0][1][-2:] == ["--request-id", "repair"]
    assert "--job-id" not in seen[0][1]
    assert seen[0][3] != "repair"
    result = call(server, "afxx_run", {"command": "issue verify", "request_id": "repair", "arguments": ["--issue-id", "F152", "--request-id", "other"]})
    assert result.structuredContent["error"]["code"] == "REQUEST_CONFLICT"
    assert len(seen) == 1


def test_grouped_remote_paths_and_query(cfg, monkeypatch, tmp_path):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    cfg["remote"]["state_dir"] = str(tmp_path / "remote")
    first = deploy_runs.submit(cfg, {"command": "deploy"}, "repair", True)
    second = deploy_runs.submit(cfg, {"command": "deploy"}, "repair", True)
    replay = replay_runs.submit(cfg, {"command": "replay", "seeds": ["ABC"]}, "repair", True)
    assert first["remote"].endswith("/repair/deploy/run-1")
    assert second["remote"].endswith("/repair/deploy/run-2")
    assert replay["remote"].endswith("/repair/replay/run-1")
    for data in (first, second, replay):
        remote = Path(data["remote"])
        shutil.copytree(data["local"], remote)
        identity = common.execution_id(remote)
        assert identity != "run-1" and identity != "run-2"
        common.phase(remote, "test")
        assert common.read(remote/"status.json")["job"] == identity
        common.write(remote/"status.json", {"job": identity, "state": "success"})
    monkeypatch.setattr(jobs, "remote", lambda c, argv: json.loads(subprocess.check_output([sys.executable, *argv[1:]], text=True)))
    assert jobs.query_job(cfg, first["job"])["job"] == first["job"]
    assert jobs.query_status(cfg, second["job"])["job_id"] == second["job"]
    # History resolves the saved execution path, not a guessed state_dir/UUID.
    run_groups.update(cfg, "repair", 1, "deploy", dry_run=False)
    assert deploy_runs.show(cfg, "repair", 1)["state"] == "success"


def test_remote_path_collision_is_rejected_before_upload(cfg, monkeypatch, tmp_path):
    import json
    import subprocess
    from pathlib import Path
    cfg["remote"]["state_dir"] = str(tmp_path/"remote")
    path = Path(cfg["remote"]["state_dir"])/"repair"/"deploy"/"run-1"
    common.write(path/"request.json", {"execution_id": "other"})
    monkeypatch.setattr(jobs, "remote", lambda c, argv: json.loads(subprocess.check_output([sys.executable, *argv[1:]], text=True)))
    original_run = common.run
    def run(argv, **kwargs):
        assert argv[0] != "npursync", "must not overwrite existing remote run"
        return original_run(argv, **kwargs)
    monkeypatch.setattr(common, "run", run)
    with pytest.raises(common.WorkflowError) as caught:
        deploy_runs.submit(cfg, {"command": "deploy"}, "repair")
    assert caught.value.code == "REMOTE_PATH_CONFLICT"
    assert common.read(path/"request.json")["execution_id"] == "other"


@pytest.mark.parametrize("group,run", [("../escape", 1), ("repair", 0), ("repair", True)])
def test_grouped_path_rejects_invalid_components(group, run):
    with pytest.raises(common.WorkflowError):
        common.grouped_path({"command": "deploy", "request_id": group, "run": run}, "execution")
