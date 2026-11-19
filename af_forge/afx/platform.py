# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""AFX repairs protocol and remote retest submission."""

import sys
from af_forge import common as workflow_common
from af_forge import config as workflow_config
from af_forge.environment import runtime as workflow_runtime

import contextlib
import json
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(c, method, path, body=None, *, remote=False):
    p = workflow_config.platform_settings(c)
    try:
        token = (
            Path(p["remote_token_file" if remote else "token_file"]).read_text().strip()
        )
    except OSError:
        raise workflow_common.WorkflowError(
            "PLATFORM_CREDENTIAL_MISSING", "无法读取平台凭据文件"
        ) from None
    workflow_common.require(
        token and not any(ch.isspace() for ch in token), "invalid platform token file"
    )
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    request = urllib.request.Request(
        p["url"].rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=30
        ) as response:
            value = json.load(response)
    except urllib.error.HTTPError as error:
        # Return only a validated protocol code, never arbitrary response text or headers.
        try:
            code = json.load(error).get("error", {}).get("code", "")
        except (ValueError, AttributeError):
            code = ""
        code = (
            code
            if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code)
            else "http_error"
        )
        raise workflow_common.WorkflowError(
            "PLATFORM_" + code.upper(), f"平台请求失败，HTTP {error.code} ({code})"
        ) from None
    except (OSError, ValueError):
        raise workflow_common.WorkflowError(
            "PLATFORM_UNAVAILABLE", "平台连接或响应异常；重试同一请求"
        ) from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("ok") is not True
        or "data" not in value
    ):
        raise workflow_common.WorkflowError(
            "PLATFORM_RESPONSE_INVALID", "平台未返回成功的 JSON 响应"
        )
    return value["data"]


API_ROOT = "/api/agent/repairs"


def identity(value):
    workflow_common.require(isinstance(value, str), "repair/retest ID must be a UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise ValueError("repair/retest ID must be a UUID") from None
    workflow_common.require(
        value == parsed, "repair/retest ID must be a canonical UUID"
    )
    return value


NUMBERED_ISSUE = re.compile(r"[FAP][0-9]{1,9}")


def validate_issue_id(value):
    workflow_common.require(
        isinstance(value, str) and NUMBERED_ISSUE.fullmatch(value),
        "issue ID must be an F, A or P followed by 1 to 9 digits",
    )
    return value


def repair_identity(value):
    return (
        value
        if isinstance(value, str) and NUMBERED_ISSUE.fullmatch(value)
        else identity(value)
    )


def record_path(c, repair):
    return (
        Path(c["local"]["state_dir"]) / "repairs" / (repair_identity(repair) + ".json")
    )


def claimed(c, repair):
    value = workflow_common.read(record_path(c, repair))
    workflow_common.require(
        value["platform_url"] == workflow_config.platform_settings(c)["url"]
        and value["agent_id"] == c["platform"]["agent_id"],
        "repair belongs to another platform or agent",
    )
    return value


def claim(c, issue_key, request_key, *, issue_id=None):
    p = workflow_config.platform_settings(c)
    body = {"agent_id": p["agent_id"], "request_key": request_key}
    if issue_key is not None:
        workflow_common.require(
            re.fullmatch(r"[0-9a-f]{24}", issue_key),
            "issue key must contain 24 lowercase hex digits",
        )
        body["issue_key"] = issue_key
    if issue_id is not None:
        workflow_common.require(issue_key is None, "specify only one issue selector")
        body["issue_id"] = validate_issue_id(issue_id)
    root = "/api/issues" if issue_id is not None else API_ROOT
    value = call(c, "POST", root + "/claim", body)
    if value is not None:
        workflow_common.require(
            isinstance(value, dict) and value.get("lease_id"), "invalid claim response"
        )
        if issue_id is not None:
            workflow_common.require(
                value.get("issue_id") == issue_id
                and isinstance(value.get("lease_expires_at"), int)
                and isinstance(value.get("repro"), dict),
                "invalid numbered issue claim response",
            )
            value.update(
                id=issue_id,
                expires=value["lease_expires_at"] / 1000,
                request_id=request_key,
            )
        else:
            identity(value["id"])
        path = record_path(c, value["id"])
        value.update(platform_url=p["url"], agent_id=p["agent_id"])
        with workflow_common.lock(path.with_suffix(".lock")):
            if path.exists():
                prior = workflow_common.read(path)
                workflow_common.require(
                    prior["expires"] <= value["expires"]
                    or prior["lease_id"] == value["lease_id"],
                    "stale claim retry; newer lease exists locally",
                )
            value["claim_request_key"] = request_key
            workflow_common.write(path, value)
    return value


def numbered_seeds(c, issue_id, repro):
    if not repro["truncated"]:
        return list(
            dict.fromkeys(
                case["case_key"]
                for case in repro["cases"]
                if case["case_source"] == "nnsmith"
            )
        )
    # The paginated endpoint omits case_source. AFX reserves short E/F keys for nnsmith.
    seeds = {}
    cursor = 0
    while True:
        page = call(
            c, "GET", f"/api/issues/{issue_id}/cases?role=affected&after={cursor}"
        )
        for case in page["cases"]:
            key = case["case_key"]
            if not case["stale"] and re.fullmatch(r"[EF][0-9A-Z]{26}", key):
                seeds[key] = None
        following = page["next_cursor"]
        if following is None:
            return list(seeds)
        workflow_common.require_code(
            type(following) is int and following > cursor,
            "PLATFORM_RESPONSE_INVALID",
            "invalid issue cases cursor",
        )
        cursor = following


def preview(c, repair, *, full=False):
    if NUMBERED_ISSUE.fullmatch(repair):
        receipt = claimed(c, repair)
        if not full:
            cases = receipt["repro"]["cases"]
            summary = receipt.get("log_summary") or next(
                (case["log_summary"] for case in cases if case.get("log_summary")),
                receipt.get("title"),
            )
            return {
                "issue_id": repair,
                "failure_summary": summary,
                "seeds": numbered_seeds(c, repair, receipt["repro"]),
            }
        return {
            "issue_id": repair,
            "snapshot": receipt["repro"],
            "related_knowledge": receipt.get("related_knowledge", []),
        }
    detail = call(c, "GET", API_ROOT + "/" + identity(repair))
    workflow_common.require(detail["id"] == repair, "repair identity mismatch")
    return {
        "repair_id": repair,
        "issue_key": detail["issue_key"],
        "snapshot": detail["snapshot"],
    }


def renew(c, lease, *, remote=False):
    body = {"lease_id": lease["lease_id"], "request_key": uuid.uuid4().hex}
    return call(
        c,
        "POST",
        ("/api/issues" if NUMBERED_ISSUE.fullmatch(lease["id"]) else API_ROOT)
        + "/"
        + repair_identity(lease["id"])
        + "/renew",
        body,
        remote=remote,
    )


@contextlib.contextmanager
def deployment_lease(c, job):
    request = workflow_common.read(job / "request.json")
    lease = request.get("issue")
    if not lease:
        yield
        return
    workflow_config.platform_settings(c)
    renew(c, lease, remote=True)
    stopped = threading.Event()

    def keep_alive():
        while not stopped.wait(30):
            try:
                renew(c, lease, remote=True)
            except Exception:
                print("平台租约续期失败，提交前将再次检查", flush=True)

    thread = threading.Thread(target=keep_alive, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=35)


def submit_issue_round(c, path, state, lease, package):
    issue_id = lease["id"]
    # Match the version representation advertised by AFX platform clients.
    candidate = {
        "lease_id": lease["lease_id"],
        "fix_version": re.sub(r"[^A-Za-z0-9_.+-]", "_", package["version"])[:64],
        "fix_sha256": package["sha256"],
    }
    if not state:
        state = {
            "state": "prepared",
            "issue_id": issue_id,
            "body": dict(candidate, request_key=uuid.uuid4().hex),
        }
        workflow_common.write(path, state)
    workflow_common.require(
        all(state["body"].get(k) == v for k, v in candidate.items()),
        "prepared verification belongs to another lease or AF package",
    )
    result = call(
        c, "POST", f"/api/issues/{issue_id}/verify", state["body"], remote=True
    )
    workflow_common.require_code(
        isinstance(result, dict)
        and result.get("issue_id") == issue_id
        and type(result.get("round_no")) is int
        and result["round_no"] > 0,
        "PLATFORM_RESPONSE_INVALID",
        "invalid numbered issue verification response",
    )
    state.update(state="submitted", round_no=result["round_no"], response=result)
    workflow_common.write(path, state)
    return state


def verify(c, job, lease=None):
    linked = workflow_common.read(job / "request.json").get("issue")
    if (
        linked
        and lease
        and (
            NUMBERED_ISSUE.fullmatch(linked["id"])
            or NUMBERED_ISSUE.fullmatch(lease["id"])
        )
    ):
        workflow_common.require(
            linked == lease, "deployment belongs to a different issue or lease"
        )
    lease = lease or linked
    workflow_common.require(
        lease, "issue-verify requires --repair or a repair-linked deployment"
    )
    repair = repair_identity(lease["id"])
    path = job / "platform.json"
    with workflow_common.lock(job / "platform.lock"):
        state = workflow_common.read(path) if path.exists() else {}
        workflow_common.require(
            not state or state.get("issue_id", state.get("repair_id")) == repair,
            "deployment already linked to another repair",
        )
        if state.get("state") == "submitted":
            if NUMBERED_ISSUE.fullmatch(repair):
                workflow_common.require(
                    state["body"]["lease_id"] == lease["lease_id"],
                    "submission belongs to another lease",
                )
            return state
        # Capture the selected binary afresh, including retries of an uncertain POST.
        code = "import json; from autofusex.fuzz.client.environment import collect; print(json.dumps(collect()))"
        package = workflow_runtime.af_identity(
            json.loads(
                workflow_common.capture(
                    [workflow_runtime.python(c), "-c", code],
                    env=workflow_runtime.environment(c),
                ).splitlines()[-1]
            )
        )
        deployment = workflow_runtime.current_deployment(c, job / "deployment.json")
        workflow_common.require(
            package["sha256"] == deployment["runtime"]["pyautofuse"]["sha256"],
            "installed AF differs from this deployment",
        )
        if NUMBERED_ISSUE.fullmatch(repair):
            return submit_issue_round(c, path, state, lease, package)
        from af_forge.afx import client as platform_client

        client = platform_client.status(c)
        workflow_common.require(
            client["state"] == "ready", "managed retest client is not ready"
        )
        workflow_common.require(
            client.get("installed", {}).get("af-mlir") == package,
            "managed client does not match the verification candidate",
        )
        client_id = identity(client["client_id"])
        if not state:
            artifact = dict(package, installed_at=deployment["installed_at"])
            body = {
                "lease_id": lease["lease_id"],
                "client_id": client_id,
                "artifact": artifact,
                "request_key": uuid.uuid4().hex,
            }
            state = {"state": "prepared", "repair_id": repair, "body": body}
            workflow_common.write(path, state)
        workflow_common.require(
            state["body"]["lease_id"] == lease["lease_id"],
            "prepared request belongs to a different lease",
        )
        workflow_common.require(
            state["body"]["client_id"] == client_id
            and state["body"]["artifact"]
            == dict(package, installed_at=deployment["installed_at"]),
            "prepared request no longer matches client or installed artifact",
        )
        # The client must be visible under the same user as the repair token.
        clients = call(c, "GET", API_ROOT + "/clients", remote=True)["items"]
        workflow_common.require(
            any(
                item["id"] == client_id
                and item["online"]
                and "af-mlir" in item["backends"]
                for item in clients
            ),
            "managed retest client is not online for this platform user",
        )
        response = call(
            c, "POST", API_ROOT + "/" + repair + "/retest", state["body"], remote=True
        )
        workflow_common.require(
            isinstance(response, dict)
            and response.get("id") == repair
            and isinstance(response.get("retest"), dict),
            "invalid retest response",
        )
        retest_id = identity(response["retest"]["id"])
        state.update(state="submitted", retest_id=retest_id, response=response)
        workflow_common.write(path, state)
        return state


def poll(c, repair, retest_id):
    identity(retest_id)
    detail = call(c, "GET", API_ROOT + "/" + identity(repair))
    workflow_common.require(detail["id"] == repair, "repair identity mismatch")
    retest = detail.get("retest")
    if not retest or retest["id"] != retest_id:
        raise workflow_common.WorkflowError(
            "PLATFORM_RETEST_SUPERSEDED",
            "指定复测已不再是当前批次；不会将其他修复的结果归到本次提交",
            detail,
        )
    workflow_common.require(
        type(retest["terminal"]) is bool and type(retest["passed"]) is bool,
        "invalid retest result",
    )
    return {
        "repair_id": repair,
        "status": detail["state"],
        "retest_id": retest_id,
        "retest": retest,
        "terminal": retest["terminal"],
        "outcome": ("passed" if retest["passed"] else "failed")
        if retest["terminal"]
        else None,
    }


def wait(c, repair, retest_id, timeout):
    deadline = time.monotonic() + timeout
    lease = claimed(c, repair)
    while True:
        result = poll(c, repair, retest_id)
        if result["terminal"]:
            if result["outcome"] != "passed":
                raise workflow_common.WorkflowError(
                    "PLATFORM_VERIFICATION_FAILED", "平台复测未通过", result
                )
            return result
        if time.monotonic() >= deadline:
            raise workflow_common.WorkflowError(
                "PLATFORM_WAIT_TIMEOUT", "等待超时，可继续查询同一复测 ID", result
            )
        renew(c, lease)
        print(f"{repair}: {result['status']}", file=sys.stderr, flush=True)
        time.sleep(min(30, max(0, deadline - time.monotonic())))


def retry_remote(job, lease=None):
    job = Path(job)
    try:
        c = workflow_config.validate_config(
            workflow_config.read_config(job / "config.yaml")
        )
        from af_forge.afx import client as platform_client

        with workflow_common.lock(platform_client.directory(c) / "control.lock"):
            with (
                workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
                workflow_common.lock(
                    workflow_runtime.registry(c).with_suffix(".lock"), shared=True
                ),
            ):
                lease = lease or workflow_common.read(job / "request.json").get("issue")
                if not lease or not NUMBERED_ISSUE.fullmatch(lease["id"]):
                    platform_client.start(c)
                value = verify(c, job, lease)
                workflow_common.require(
                    value is not None, "job is not linked to an issue"
                )
        return {"state": "success", "verification": value}
    except workflow_common.WorkflowError as error:
        return {"state": "failed", "error_code": error.code, "error": str(error)}
    except Exception as error:
        return {
            "state": "failed",
            "error_code": "PLATFORM_VERIFY_FAILED",
            "error": str(error),
        }
