# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
import af_forge.cli
import af_forge.cli as w
from af_forge import common as workflow_common
from af_forge import config as workflow_config
from af_forge import jobs as workflow_jobs
from af_forge.environment import runtime as workflow_runtime

import json
from pathlib import Path
import sys

import pytest
from af_forge.afx import platform as p

REPAIR = "F152"
RETEST = "22222222-2222-4222-8222-222222222222"
CLIENT = "33333333-3333-4333-8333-333333333333"
ISSUE = "a" * 24
PACKAGE = {"version": "9.3", "sha256": "a" * 64}
LEASE = {"id": REPAIR, "lease_id": "lease"}


@pytest.fixture
def platform_cfg(cfg, tmp_path):
    cfg["platform"] = dict(
        url="https://platform.example",
        agent_id="agent-1",
        token_file=str(tmp_path / "token"),
        remote_token_file=str(tmp_path / "remote-token"),
    )
    (tmp_path / "token").write_text("local-secret")
    (tmp_path / "remote-token").write_text("remote-secret")
    return cfg


def test_claim_preview_and_renew_current_protocol(platform_cfg, monkeypatch):
    calls = []

    def api(c, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return dict(
            issue_id=REPAIR,
            lease_id="lease",
            lease_expires_at=999999000,
            repro={"cases": [], "truncated": False},
        )

    monkeypatch.setattr(p, "call", api)
    p.claim(platform_cfg, "claim-key")
    assert calls[0] == (
        "POST",
        "/api/issues/claim",
        dict(agent_id="agent-1", request_key="claim-key"),
    )
    assert p.preview(platform_cfg, REPAIR)["seeds"] == []
    p.renew(platform_cfg, LEASE)
    assert calls[-1][1] == "/api/issues/F152/renew"
    assert "secret" not in p.record_path(platform_cfg, REPAIR).read_text()


def test_empty_claim_and_rejected_uuid(platform_cfg, monkeypatch):
    monkeypatch.setattr(p, "call", lambda *a, **k: None)
    assert p.claim(platform_cfg, "empty") is None
    with pytest.raises(ValueError, match="issue ID"):
        p.claim(platform_cfg, "key", issue_id=CLIENT)


@pytest.fixture
def candidate(platform_cfg, tmp_path, monkeypatch):
    job = tmp_path / "job"
    workflow_common.write(job / "request.json", {"issue": LEASE})
    deployment = {
        "runtime": {"pyautofuse": {"sha256": "a" * 64}},
        "installed_at": "2026-11-18T00:00:00+00:00",
    }
    workflow_common.write(job / "deployment.json", deployment)
    monkeypatch.setattr(
        workflow_runtime, "current_deployment", lambda c, path: deployment
    )
    monkeypatch.setattr(workflow_runtime, "environment", lambda c: {})
    monkeypatch.setattr(
        workflow_common,
        "capture",
        lambda *a, **k: json.dumps({"cann": "9.3", "autofuse_binary": "a" * 64}),
    )
    return job


def receipt():
    return {"issue_id": REPAIR, "round_no": 1, "status": "verifying"}


def test_verify_timeout_retries_frozen_body(platform_cfg, candidate, monkeypatch):
    bodies = []

    def api(c, method, path, body=None, **kwargs):
        assert (
            kwargs["remote"] and method == "POST" and path == "/api/issues/F152/verify"
        )
        bodies.append(dict(body))
        if len(bodies) == 1:
            raise workflow_common.WorkflowError("PLATFORM_UNAVAILABLE", "timeout")
        return receipt()

    monkeypatch.setattr(p, "call", api)
    with pytest.raises(workflow_common.WorkflowError):
        p.verify(platform_cfg, candidate)
    assert p.verify(platform_cfg, candidate)["round_no"] == 1
    p.verify(platform_cfg, candidate)
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert set(bodies[0]) == {"lease_id", "fix_version", "fix_sha256", "request_key"}
    assert bodies[0]["fix_sha256"] == PACKAGE["sha256"]


@pytest.mark.parametrize("failure", ["binary", "lease", "changed_retry"])
def test_verify_rejects_drift_before_post(
    platform_cfg, candidate, monkeypatch, failure
):
    if failure == "binary":
        monkeypatch.setattr(
            workflow_common,
            "capture",
            lambda *a, **k: json.dumps({"cann": "9.3", "autofuse_binary": "b" * 64}),
        )
    else:
        workflow_common.write(
            candidate / "platform.json",
            {
                "state": "prepared",
                "issue_id": REPAIR,
                "body": dict(
                    lease_id="old" if failure == "lease" else "lease",
                    request_key="frozen",
                    fix_version="9.3",
                    fix_sha256="b" * 64 if failure == "changed_retry" else "a" * 64,
                ),
            },
        )
    monkeypatch.setattr(p, "call", lambda *a, **k: pytest.fail("must not submit"))
    with pytest.raises(ValueError):
        p.verify(platform_cfg, candidate)


def test_explicit_verify_binds_unlinked_deployment(
    platform_cfg, candidate, monkeypatch
):
    workflow_common.write(candidate / "request.json", {"command": "deploy"})
    monkeypatch.setattr(p, "call", lambda *a, **k: receipt())
    assert p.verify(platform_cfg, candidate, LEASE)["round_no"] == 1
    assert workflow_common.read(candidate / "request.json") == {"command": "deploy"}
    with pytest.raises(ValueError, match="another issue"):
        p.verify(platform_cfg, candidate, dict(LEASE, id="F153"))


@pytest.mark.parametrize(
    "regress,outcome",
    [
        (False, None),
        (False, "failed"),
        (False, "passed"),
        (True, None),
        (True, "passed"),
        (True, "invalid"),
    ],
)
def test_poll_tracks_verify_and_its_regression(
    platform_cfg, monkeypatch, regress, outcome
):
    verify = dict(
        round_no=1,
        kind="verify",
        fix_version="9.3",
        fix_sha256="a" * 64,
        finished_at=1 if regress or outcome else None,
        outcome="passed" if regress else outcome,
    )
    rounds = [verify]
    if regress:
        rounds.append(
            dict(
                verify,
                round_no=2,
                kind="regress",
                finished_at=2 if outcome else None,
                outcome=outcome,
            )
        )
    # A later repair must not replace the requested round's verdict.
    rounds.append(
        dict(verify, round_no=3, kind="verify", finished_at=5, outcome="failed")
    )
    monkeypatch.setattr(
        p,
        "call",
        lambda *a, **k: dict(issue_id=REPAIR, status="claimed", rounds=rounds),
    )
    result = p.poll(platform_cfg, REPAIR, 1)
    assert result["terminal"] == (outcome is not None)
    assert result["outcome"] == outcome
    with pytest.raises(ValueError, match="round not found"):
        p.poll(platform_cfg, REPAIR, 4)


def test_wait_timeout_keeps_round_and_renews(platform_cfg, monkeypatch):
    monkeypatch.setattr(p, "claimed", lambda *a: LEASE)
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr(p.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(p.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        p, "poll", lambda *a: dict(terminal=False, status="verifying", round_no=1)
    )
    renewals = []
    monkeypatch.setattr(p, "renew", lambda *a: renewals.append(True))
    with pytest.raises(workflow_common.WorkflowError) as error:
        p.wait(platform_cfg, REPAIR, 1, 1)
    assert (
        error.value.code == "PLATFORM_WAIT_TIMEOUT"
        and error.value.data["round_no"] == 1
    )
    assert renewals == [True]


def test_http_envelope_and_remote_token(platform_cfg, monkeypatch):
    import io

    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return io.BytesIO(b'{"schema_version":1,"ok":true,"data":null}')

    monkeypatch.setattr(p.urllib.request, "build_opener", lambda *a: Opener())
    assert p.call(platform_cfg, "POST", "/api/issues/claim", {}, remote=True) is None
    assert requests[0].headers["Authorization"] == "Bearer remote-secret"
    assert (
        p.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other")
        is None
    )


def test_issue_cli_uses_http_without_npussh(platform_cfg, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["af_forge/cli.py", "issue", "preview", "--issue-id", REPAIR]
    )
    monkeypatch.setattr(workflow_config, "config", lambda path: platform_cfg)
    monkeypatch.setattr(
        workflow_jobs, "local_preflight", lambda *a: pytest.fail("must not connect")
    )
    monkeypatch.setattr(
        p, "preview", lambda c, i, **kwargs: {"issue_id": i, "snapshot": {}}
    )
    assert af_forge.cli.main()["issue_id"] == REPAIR


def test_deploy_cli_freezes_issue_lease(platform_cfg, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "af_forge/cli.py",
            "deploy",
            "--issue-id",
            REPAIR,
            "--request-id",
            "deploy-1",
            "--dry-run",
        ],
    )
    monkeypatch.setattr(workflow_config, "config", lambda path: platform_cfg)
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(p, "claimed", lambda *a: dict(LEASE, extra="unused"))
    requests = []
    monkeypatch.setattr(
        workflow_jobs,
        "submit",
        lambda c, r, *a: requests.append(r) or {"state": "prepared"},
    )
    af_forge.cli.main()
    assert requests[0]["issue"] == LEASE


def test_retry_cli_returns_remote_receipt(platform_cfg, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["af_forge/cli.py", "issue", "verify", "--job", "a" * 32]
    )
    monkeypatch.setattr(workflow_config, "config", lambda path: platform_cfg)
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *a: None)
    monkeypatch.setattr(
        workflow_jobs,
        "remote",
        lambda *a: {
            "state": "success",
            "verification": {"state": "submitted", "round_no": 1},
        },
    )
    assert af_forge.cli.main()["round_no"] == 1


def test_script_entrypoint_preserves_adapter_error_code(platform_cfg, tmp_path):
    import subprocess

    Path(platform_cfg["platform"]["token_file"]).unlink()
    config = tmp_path / "config.yaml"
    workflow_common.write(config, platform_cfg)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "af_forge.cli",
            "issue",
            "claim",
            "--config",
            str(config),
            "--issue-id",
            REPAIR,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "PLATFORM_CREDENTIAL_MISSING"
    assert "secret" not in result.stdout


@pytest.mark.parametrize(
    "failure,expected_code",
    [
        (
            workflow_common.WorkflowError(
                "PLATFORM_CREDENTIAL_MISSING", "无法读取平台凭据文件"
            ),
            "PLATFORM_CREDENTIAL_MISSING",
        ),
        (
            workflow_common.WorkflowError("PLATFORM_CONFLICT", "lease expired"),
            "PLATFORM_CONFLICT",
        ),
        (RuntimeError("unexpected client failure"), "PLATFORM_VERIFY_FAILED"),
    ],
)
def test_issue_verify_propagates_remote_failure(
    platform_cfg, monkeypatch, capsys, failure, expected_code
):
    monkeypatch.setattr(
        sys, "argv", ["workflow", "issue", "verify", "--job-id", "a" * 32]
    )
    monkeypatch.setattr(workflow_config, "config", lambda path: platform_cfg)
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *args: None)

    def fail(_path):
        raise failure

    monkeypatch.setattr(workflow_config, "read_config", fail)
    monkeypatch.setattr(
        workflow_jobs, "remote", lambda c, argv: p.retry_remote(argv[-2])
    )
    assert af_forge.cli.cli() == 2
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is False
    assert envelope["error"] == {"code": expected_code, "message": str(failure)}


def test_issue_verify_accepts_text_only_failure(platform_cfg, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["workflow", "issue", "verify", "--job-id", "a" * 32]
    )
    monkeypatch.setattr(workflow_config, "config", lambda path: platform_cfg)
    monkeypatch.setattr(workflow_jobs, "local_preflight", lambda *args: None)
    monkeypatch.setattr(
        workflow_jobs,
        "remote",
        lambda *args: {"state": "failed", "error": "old job failed"},
    )
    with pytest.raises(workflow_common.WorkflowError, match="old job failed") as exc:
        af_forge.cli.main()
    assert exc.value.code == "PLATFORM_VERIFY_FAILED"


@pytest.mark.parametrize(
    "arguments",
    [
        ["issue", "claim", "--issue-id", "F152", "--issue-key", ISSUE],
        ["issue", "claim", "--issue-id", "../F152"],
        ["issue", "preview", "--issue-id", "F152", "--repair-id", REPAIR],
    ],
)
def test_issue_selector_rejected_before_platform_request(
    monkeypatch, arguments, capsys
):
    monkeypatch.setattr(sys, "argv", ["workflow", *arguments])
    monkeypatch.setattr(
        p, "call", lambda *a, **kw: pytest.fail("unexpected platform request")
    )
    assert af_forge.cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_numbered_verify_rejects_different_issue_or_lease(
    platform_cfg, candidate, monkeypatch
):
    workflow_common.write(candidate / "request.json", {"issue": dict(LEASE, id="F152")})
    monkeypatch.setattr(
        p, "call", lambda *a, **kw: pytest.fail("unexpected verify request")
    )
    for issue_id, lease_id in [("F153", "lease"), ("F152", "other")]:
        with pytest.raises(ValueError, match="different issue or lease"):
            p.verify(platform_cfg, candidate, {"id": issue_id, "lease_id": lease_id})


def test_truncated_issue_preview_lists_all_active_seeds(platform_cfg, monkeypatch):
    seeds = ["E" + str(i).zfill(26) for i in (1, 2, 3)]

    def row(key, stale=0):
        return {"case_key": key, "stale": stale}

    pages = {
        "0": {
            "cases": [row(seeds[0]), row("imported:key"), row(seeds[2], 1)],
            "next_cursor": 3,
        },
        "3": {"cases": [row(seeds[0]), row(seeds[1])], "next_cursor": None},
    }
    monkeypatch.setattr(
        p, "call", lambda c, method, path: pages.pop(path.split("after=")[1])
    )
    assert p.numbered_seeds(platform_cfg, "F152", {"truncated": True}) == seeds[:2]
    assert not pages


@pytest.mark.parametrize("outcome", ["failed", "invalid"])
def test_wait_returns_failed_round_details(platform_cfg, monkeypatch, outcome):
    monkeypatch.setattr(p, "claimed", lambda *a: LEASE)
    result = dict(terminal=True, outcome=outcome, round_no=1)
    monkeypatch.setattr(p, "poll", lambda *a: result)
    with pytest.raises(workflow_common.WorkflowError) as error:
        p.wait(platform_cfg, REPAIR, 1, 30)
    assert error.value.code == "PLATFORM_VERIFICATION_FAILED"
    assert error.value.data == result


def test_regression_must_match_candidate(platform_cfg, monkeypatch):
    verification = dict(round_no=1, kind="verify", fix_version="9.3", fix_sha256="a" * 64,
                        finished_at=1, outcome="passed")
    regression = dict(verification, round_no=2, kind="regress", fix_sha256="b" * 64)
    monkeypatch.setattr(p, "call", lambda *a, **k: dict(issue_id=REPAIR, status="summarizing", rounds=[verification, regression]))
    with pytest.raises(ValueError, match="candidate differs"):
        p.poll(platform_cfg, REPAIR, 1)
