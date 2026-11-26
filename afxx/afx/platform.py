"""AFX issue protocol and remote verification submission."""

import sys
from afxx import common as workflow_common
from afxx import config as workflow_config
from afxx.environment import runtime as workflow_runtime

import contextlib
import json
import math
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


NUMBERED_ISSUE = re.compile(r"[FAP][0-9]{1,9}")


def validate_issue_id(value):
    workflow_common.require(
        isinstance(value, str) and NUMBERED_ISSUE.fullmatch(value),
        "issue ID must be an F, A or P followed by 1 to 9 digits",
    )
    return value


def record_path(c, issue_id):
    return (
        Path(c["local"]["state_dir"])
        / "issues"
        / (validate_issue_id(issue_id) + ".json")
    )


def claimed(c, issue_id, *, allow_released=False):
    value = workflow_common.read(record_path(c, issue_id))
    workflow_common.require(
        value["platform_url"] == workflow_config.platform_settings(c)["url"]
        and value["agent_id"] == c["platform"]["agent_id"],
        "issue belongs to another platform or agent",
    )
    workflow_common.require_code(allow_released or not value.get("release"),
                                 "ISSUE_RELEASED", "issue lease has been released; claim again")
    return value


def claim(c, request_key, *, issue_id=None):
    p = workflow_config.platform_settings(c)
    body = {"agent_id": p["agent_id"], "request_key": request_key}
    if issue_id is not None:
        body["issue_id"] = validate_issue_id(issue_id)
    value = call(c, "POST", "/api/issues/claim", body)
    if value is None:
        return None
    workflow_common.require(
        isinstance(value, dict)
        and value.get("lease_id")
        and isinstance(value.get("lease_expires_at"), int)
        and isinstance(value.get("repro"), dict),
        "invalid issue claim response",
    )
    selected = validate_issue_id(value.get("issue_id"))
    workflow_common.require(
        issue_id is None or selected == issue_id, "issue identity mismatch"
    )
    value.update(
        id=selected,
        expires=value["lease_expires_at"] / 1000,
        request_id=request_key,
        platform_url=p["url"],
        agent_id=p["agent_id"],
    )
    path = record_path(c, selected)
    with workflow_common.lock(path.with_suffix(".lock")):
        if path.exists():
            prior = workflow_common.read(path)
            workflow_common.require(
                prior["expires"] <= value["expires"]
                or prior["lease_id"] == value["lease_id"],
                "stale claim retry; newer lease exists locally",
            )
        if path.exists() and prior.get("release") and prior["lease_id"] == value["lease_id"]:
            raise workflow_common.WorkflowError("ISSUE_RELEASED", "claim retry refers to a released lease; use a new request-id")
        workflow_common.write(path, value)
    return value


def summarize(c, issue_id, summary_file, request_id):
    validate_issue_id(issue_id)
    workflow_common.require(isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id),
                            "summarize requires a valid request-id")
    try:
        knowledge = workflow_config.read_config(summary_file)
    except (OSError, ValueError, TypeError) as error:
        raise workflow_common.WorkflowError("SUMMARY_INVALID", f"cannot read summary YAML: {error}") from error
    required = {"kind", "title", "symptom", "root_cause", "solution"}
    optional = {"keywords", "ops", "dtypes", "backends", "graph_pattern", "applicability", "version_range", "fix_ref", "body_md"}
    workflow_common.require_code(
        isinstance(knowledge, dict)
        and all(isinstance(knowledge.get(k), str) and knowledge[k].strip() for k in required)
        and set(knowledge) <= required | optional,
        "SUMMARY_INVALID", "summary must contain kind/title/symptom/root_cause/solution and only supported knowledge fields",
    )
    lease = claimed(c, issue_id)
    body = {"request_key": request_id, "lease_id": lease["lease_id"], "knowledge": knowledge}
    context = {"platform_url": workflow_config.platform_settings(c)["url"], "agent_id": c["platform"]["agent_id"]}
    path = record_path(c, issue_id).with_suffix("") / "summaries" / (workflow_common.digest(request_id) + ".json")
    with workflow_common.lock(path.with_suffix(".lock")):
        state = workflow_common.read(path) if path.exists() else {}
        if state:
            workflow_common.require_code(state["body"] == body and state["context"] == context,
                                         "REQUEST_CONFLICT", "summary request-id already binds another summary, lease or platform")
            if state["state"] == "submitted":
                return state["response"]
        else:
            state = {"state": "prepared", "issue_id": issue_id, "context": context, "body": body}
            workflow_common.write(path, state)
        # Let the platform enforce summarizing, lease and knowledge permission.
        # Do not pre-check state: a lost successful response must remain retryable after resolved.
        value = call(c, "POST", f"/api/issues/{issue_id}/knowledge", state["body"])
        workflow_common.require_code(
            isinstance(value, dict) and value.get("issue_id") == issue_id
            and value.get("status") == "resolved" and isinstance(value.get("knowledge_id"), str)
            and re.fullmatch(r"K[0-9]{1,9}", value["knowledge_id"]),
            "PLATFORM_RESPONSE_INVALID", "invalid summary publication response",
        )
        state.update(state="submitted", response=value)
        workflow_common.write(path, state)
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


def preview(c, issue_id, *, full=False):
    validate_issue_id(issue_id)
    receipt = claimed(c, issue_id)
    if not full:
        cases = receipt["repro"]["cases"]
        summary = receipt.get("log_summary") or next(
            (case["log_summary"] for case in cases if case.get("log_summary")),
            receipt.get("title"),
        )
        return {
            "issue_id": issue_id,
            "failure_summary": summary,
            "seeds": numbered_seeds(c, issue_id, receipt["repro"]),
        }
    return {
        "issue_id": issue_id,
        "snapshot": receipt["repro"],
        "related_knowledge": receipt.get("related_knowledge", []),
    }


def renew(c, lease, *, remote=False):
    body = {"lease_id": lease["lease_id"], "request_key": uuid.uuid4().hex}
    return call(
        c,
        "POST",
        "/api/issues" + "/" + validate_issue_id(lease["id"]) + "/renew",
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
    from afxx.afx import launcher

    launcher.ensure_started(c, package)
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
    if linked and lease:
        workflow_common.require(
            linked == lease, "deployment belongs to a different issue or lease"
        )
    lease = lease or linked
    workflow_common.require(
        lease, "issue verify requires --issue-id or an issue-linked deployment"
    )
    issue_id = validate_issue_id(lease["id"])
    path = job / "platform.json"
    with workflow_common.lock(job / "platform.lock"):
        state = workflow_common.read(path) if path.exists() else {}
        workflow_common.require(
            not state or state.get("issue_id") == issue_id,
            "deployment already linked to another issue",
        )
        if state.get("state") == "submitted":
            workflow_common.require(
                state["body"]["lease_id"] == lease["lease_id"],
                "submission belongs to another lease",
            )
            return state
        # Capture the selected binary afresh, including retries of an uncertain POST.
        code = "import json; from autofusex.execution.environment import collect; print(json.dumps(collect()))"
        package = workflow_runtime.af_identity(
            json.loads(
                workflow_common.capture(
                    [workflow_runtime.python(c), "-c", code],
                    env=workflow_runtime.environment(c),
                ).splitlines()[-1]
            )
        )
        return submit_issue_round(c, path, state, lease, package)


def issue_detail(c, issue_id):
    validate_issue_id(issue_id)
    value = call(c, "GET", f"/api/issues/{issue_id}")
    workflow_common.require_code(
        isinstance(value, dict) and value.get("issue_id") == issue_id,
        "PLATFORM_RESPONSE_INVALID", "issue identity mismatch",
    )
    return value


def issue_tasks(c, issue_id, round_no):
    validate_issue_id(issue_id)
    workflow_common.require(type(round_no) is int and 1 <= round_no <= 999999,
                            "round number must be between 1 and 999999")
    value = call(c, "GET", f"/api/issues/{issue_id}/rounds/{round_no}/tasks")
    workflow_common.require_code(isinstance(value, list) and all(isinstance(task, dict) for task in value),
                                 "PLATFORM_RESPONSE_INVALID", "invalid round tasks response")
    return {"issue_id": issue_id, "round_no": round_no, "tasks": value}


def regression_preview(c, issue_id):
    validate_issue_id(issue_id)
    value = call(c, "GET", "/api/regression/preview?" + urllib.parse.urlencode({"issue_id": issue_id}))
    workflow_common.require_code(
        isinstance(value, dict) and value.get("issue_id") == issue_id,
        "PLATFORM_RESPONSE_INVALID", "regression preview identity mismatch",
    )
    return value


def execution_detail(c, exec_uuid):
    # AFX ingestion accepts opaque execution IDs, not only canonical UUIDs.
    workflow_common.require(isinstance(exec_uuid, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", exec_uuid),
                            "execution UUID must contain 8 to 64 letters, digits, underscores or hyphens")
    value = call(c, "GET", "/api/executions/" + exec_uuid)
    workflow_common.require_code(
        isinstance(value, dict) and value.get("exec_uuid") == exec_uuid,
        "PLATFORM_RESPONSE_INVALID", "execution identity mismatch",
    )
    return value


def poll(c, issue_id, round_no, *, include_tasks=False):
    validate_issue_id(issue_id)
    workflow_common.require(
        type(round_no) is int and round_no > 0, "round number must be positive"
    )
    return round_status(c, issue_detail(c, issue_id), issue_id, round_no, include_tasks=include_tasks)


def round_status(c, detail, issue_id, round_no, *, include_tasks=False):
    rounds = detail["rounds"]
    verification = next((r for r in rounds if r["round_no"] == round_no), None)
    workflow_common.require(
        verification and verification["kind"] == "verify",
        "verification round not found",
    )
    regression = next(
        (r for r in rounds if r["round_no"] == round_no + 1 and r["kind"] == "regress"),
        None,
    )
    if regression:
        workflow_common.require(
            all(
                regression[k] == verification[k] for k in ("fix_version", "fix_sha256")
            ),
            "regression candidate differs from verification",
        )
    if include_tasks:
        verification = dict(verification, tasks=issue_tasks(c, issue_id, verification["round_no"])["tasks"])
        if regression:
            regression = dict(regression, tasks=issue_tasks(c, issue_id, regression["round_no"])["tasks"])
    last = regression or verification
    terminal = last["finished_at"] is not None
    return {
        "issue_id": issue_id,
        "status": detail["status"],
        "round_no": round_no,
        "verification": verification,
        "regression": regression,
        "terminal": terminal,
        "outcome": last["outcome"] if terminal else None,
    }


def wait(c, issue_id, round_no, timeout):
    deadline = time.monotonic() + timeout
    lease = claimed(c, issue_id)
    previous = None
    while True:
        result = poll(c, issue_id, round_no)
        if result["terminal"]:
            if result["outcome"] != "passed":
                raise workflow_common.WorkflowError(
                    "PLATFORM_VERIFICATION_FAILED", "平台复测未通过", result
                )
            return result
        if time.monotonic() >= deadline:
            raise workflow_common.WorkflowError(
                "PLATFORM_WAIT_TIMEOUT", "等待超时，可继续查询同一验证轮次",
                {"issue_id": issue_id, "round_no": round_no, "event": "timeout"}
            )
        renew(c, lease)
        if previous is not None and result['status'] != previous:
            print(f"{issue_id}: {result['status']}", file=sys.stderr, flush=True)
        previous = result['status']
        time.sleep(min(30, max(0, deadline - time.monotonic())))


def retry_remote(job, lease=None):
    job = Path(job)
    try:
        c = workflow_config.validate_config(
            workflow_config.read_config(job / "config.yaml")
        )
        with (
            workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
            workflow_common.lock(
                workflow_runtime.registry(c).with_suffix(".lock"), shared=True
            ),
        ):
            value = verify(c, job, lease)
        return {"state": "success", "verification": value}
    except workflow_common.WorkflowError as error:
        return {"state": "failed", "error_code": error.code, "error": str(error)}
    except Exception as error:
        return {
            "state": "failed",
            "error_code": "PLATFORM_VERIFY_FAILED",
            "error": str(error),
        }


def release(c, issue_id, request_id):
    """Release the claimed lease; retain the successful receipt for safe retries."""
    workflow_common.require(isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id),
                            "release requires a valid request-id")
    path = record_path(c, issue_id)
    with workflow_common.lock(path.with_suffix(".lock")):
        lease = claimed(c, issue_id, allow_released=True)
        receipt = path.with_suffix("") / "releases" / (workflow_common.digest(request_id) + ".json")
        body = {"lease_id": lease["lease_id"], "request_key": request_id}
        context = {key: lease[key] for key in ("platform_url", "agent_id")}
        state = workflow_common.read(receipt) if receipt.exists() else {}
        if state:
            workflow_common.require_code(state["body"] == body and state["context"] == context,
                                         "REQUEST_CONFLICT", "release request-id belongs to another lease or platform")
        else:
            workflow_common.require_code(not lease.get("release"), "ISSUE_RELEASED", "lease already released")
            state = {"body": body, "context": context}
            workflow_common.write(receipt, state)
        if "response" not in state:
            value = call(c, "POST", f"/api/issues/{issue_id}/release", body)
            workflow_common.require_code(isinstance(value, dict) and value.get("issue_id") == issue_id
                                         and isinstance(value.get("status"), str),
                                         "PLATFORM_RESPONSE_INVALID", "invalid release response")
            state["response"] = value
            workflow_common.write(receipt, state)
        lease["release"] = {"request_id": request_id, "response": state["response"]}
        workflow_common.write(path, lease)
        return state["response"]


def case_path(case_key):
    workflow_common.require(isinstance(case_key, str) and case_key.strip() == case_key
                            and bool(case_key) and case_key not in (".", "..")
                            and not any(ch.isspace() or ord(ch) < 32 for ch in case_key),
                            "invalid case key")
    return "/api/cases/" + urllib.parse.quote(case_key, safe="")


def case_spec(c, case_key):
    value = call(c, "GET", case_path(case_key) + "/spec")
    workflow_common.require_code(isinstance(value, dict), "PLATFORM_RESPONSE_INVALID", "invalid case spec")
    return value


def case_history(c, case_key, *, after=None, limit=50):
    path = case_path(case_key)
    workflow_common.require(type(limit) is int and 1 <= limit <= 200, "limit must be 1..200")
    workflow_common.require(after is None or (type(after) is int and after >= 0), "after must be a nonnegative cursor")
    params = {"limit": limit}
    if after is not None:
        params["after"] = after
    value = call(c, "GET", path + "/history?" + urllib.parse.urlencode(params))
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("case"), dict)
                                 and value["case"].get("case_key") == case_key
                                 and isinstance(value.get("executions"), list)
                                 and type(value.get("total")) is int
                                 and "next_cursor" in value
                                 and (value["next_cursor"] is None or type(value["next_cursor"]) is int),
                                 "PLATFORM_RESPONSE_INVALID", "invalid case history")
    return value


def retest_list(c):
    value = call(c, "GET", "/api/retests")
    workflow_common.require_code(isinstance(value, list), "PLATFORM_RESPONSE_INVALID", "invalid retests list")
    return value


def issue_list(c, *, dimension=None, backend=None, status=None, q=None, after=None, limit=50):
    workflow_common.require(type(limit) is int and 1 <= limit <= 200, "limit must be 1..200")
    params = {"limit": limit}
    for key, value in (("dimension", dimension), ("backend", backend), ("status", status), ("after", after)):
        if value is not None:
            workflow_common.require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+<>=-]{1,256}", value), "invalid " + key)
            params[key] = value
    if q is not None:
        workflow_common.require(isinstance(q, str) and 1 <= len(q) <= 128 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in q), "invalid q")
        params["q"] = q
    value = call(c, "GET", "/api/issues?" + urllib.parse.urlencode(params))
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("issues"), list)
                                 and "next_cursor" in value and (value["next_cursor"] is None or isinstance(value["next_cursor"], str)),
                                 "PLATFORM_RESPONSE_INVALID", "invalid issue list")
    return value


def issue_cases(c, issue_id, *, role="affected", after=None):
    validate_issue_id(issue_id)
    workflow_common.require(role in ("affected", "regression"), "role must be affected or regression")
    workflow_common.require(after is None or (type(after) is int and after >= 0), "after must be a nonnegative cursor")
    params = {"role": role}
    if after is not None:
        params["after"] = after
    value = call(c, "GET", f"/api/issues/{issue_id}/cases?" + urllib.parse.urlencode(params))
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("cases"), list)
                                 and "next_cursor" in value and (value["next_cursor"] is None or type(value["next_cursor"]) is int),
                                 "PLATFORM_RESPONSE_INVALID", "invalid issue cases")
    return value


def platform_me(c):
    value = call(c, "GET", "/api/me")
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("user_id"), str)
                                 and isinstance(value.get("role"), str), "PLATFORM_RESPONSE_INVALID", "invalid platform identity")
    return value


def platform_summary(c, *, scope="mine"):
    workflow_common.require(scope in ("mine", "all"), "scope must be mine or all")
    value = call(c, "GET", "/api/ops/summary?" + urllib.parse.urlencode({"scope": scope}))
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("clients"), list),
                                 "PLATFORM_RESPONSE_INVALID", "invalid platform summary")
    return value


def case_list(c, *, backend=None, status=None, arch=None, case_key=None, case_id=None, q=None, after=None, limit=50):
    workflow_common.require(type(limit) is int and 1 <= limit <= 200, "limit must be 1..200")
    workflow_common.require(after is None or (type(after) is int and after >= 0), "after must be a nonnegative cursor")
    workflow_common.require(case_id is None or (type(case_id) is int and case_id > 0), "case-id must be positive")
    workflow_common.require(case_id is None or case_key is None, "case-id and case-key are mutually exclusive")
    exact = case_id is not None or case_key is not None
    workflow_common.require(not exact or all(v is None for v in (backend, status, arch, q, after)),
                            "exact case lookup cannot combine filters or after")
    workflow_common.require(q is None or all(v is None for v in (backend, status, arch)),
                            "combine backend/status/arch into q instead of separate filters")
    workflow_common.require(status is None or status in ("pass", "fail", "function_fail", "accuracy_fail"), "invalid status")
    workflow_common.require((status is None and arch is None) or backend is not None, "status/arch require backend")
    params = {"limit": limit}
    for key, value in (("backend", backend), ("status", status), ("arch", arch), ("case_key", case_key)):
        if value is not None:
            workflow_common.require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/+<>=-]{1,256}", value), "invalid " + key)
            params[key] = value
    if q is not None:
        workflow_common.require(isinstance(q, str) and bool(q.strip()) and len(q) <= 128
                                and not any(ord(ch) < 32 or ord(ch) == 127 for ch in q), "invalid q")
        params["q"] = q
    for key, value in (("case_id", case_id), ("after", after)):
        if value is not None:
            params[key] = value
    value = call(c, "GET", "/api/cases?" + urllib.parse.urlencode(params))
    workflow_common.require_code(isinstance(value, dict) and isinstance(value.get("groups"), list)
                                 and all(isinstance(g, dict) and isinstance(g.get("case"), dict)
                                         and isinstance(g.get("executions"), list) and type(g.get("more_executions")) is bool
                                         for g in value["groups"])
                                 and "next_cursor" in value and (value["next_cursor"] is None or type(value["next_cursor"]) is int),
                                 "PLATFORM_RESPONSE_INVALID", "invalid case list")
    return value


def verify_check_once(c, issue_id, round_no):
    detail = issue_detail(c, issue_id)
    snapshot = round_status(c, detail, issue_id, round_no, include_tasks=True)
    verification, regression = snapshot["verification"], snapshot["regression"]
    failures = []
    summaries = {}
    for phase, rnd in (("verification", verification), ("regression", regression)):
        if rnd is None:
            summaries[phase] = None
            continue
        counts = {}
        for task in rnd["tasks"]:
            result = task.get("result") or task.get("status") or "unknown"
            counts[result] = counts.get(result, 0) + 1
            if result in ("fail", "invalid", "cancelled", "skip"):
                item = {k: task[k] for k in ("task_id", "case_key", "npu_arch", "status", "result", "exec_uuid", "executions") if k in task}
                item.update(phase=phase, round_no=rnd["round_no"], detail=str(task.get("detail") or "")[:1000])
                failures.append(item)
        summaries[phase] = {k: v for k, v in rnd.items() if k != "tasks"}
        summaries[phase].update(task_count=len(rnd["tasks"]), counts=counts)
    state, phase, reason = "running", "verification", None
    def completed_state(rnd, summary):
        if rnd["finished_at"] is None:
            return "running"
        if rnd.get("outcome") == "failed":
            return "failed"
        if rnd.get("outcome") == "invalid":
            return "invalid"
        counts = summary["counts"]
        if rnd.get("outcome") == "passed" and summary["task_count"] > 0 and set(counts) == {"pass"}:
            return "passed"
        return "attention"
    state = completed_state(verification, summaries["verification"])
    if state == "passed":
        if regression is not None:
            phase = "regression"
            state = completed_state(regression, summaries["regression"])
        else:
            newer = any(r["round_no"] > round_no for r in detail["rounds"])
            phase = "regression_selection"
            if not newer and detail["status"] in ("summarizing", "resolved"):
                reason = "no_regression_candidates"
            elif newer or detail["status"] not in ("verifying", "regressing"):
                state, reason = "attention", "cannot_confirm_regression_selection"
            else:
                state = "running"
    return dict(issue_id=issue_id, round_no=round_no, issue_status=detail["status"],
                state=state, phase=phase, reason=reason, verification=summaries["verification"],
                regression=summaries["regression"], failures=failures[:20], failure_count=len(failures),
                failures_truncated=len(failures) > 20, timed_out=False,
                next_action="wait" if state == "running" else "review_summary" if state == "passed" else "inspect")


def verify_check(c, issue_id, round_no, *, wait=False, timeout=None):
    validate_issue_id(issue_id)
    workflow_common.require(type(round_no) is int and 1 <= round_no <= 999999, "invalid round number")
    workflow_common.require(type(wait) is bool, "wait must be boolean")
    workflow_common.require(timeout is None or (wait and isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                            and math.isfinite(timeout) and 0 < timeout <= 50), "timeout requires wait and must be in (0, 50]")
    deadline = time.monotonic() + (timeout if timeout is not None else 30)
    while True:
        result = verify_check_once(c, issue_id, round_no)
        if not wait or result["state"] != "running":
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result["timed_out"] = True
            return result
        time.sleep(min(5, remaining))


def verify_current(c, request_id, lease):
    """Submit the installed package, independent of any deployment history."""
    workflow_common.validate_job_id(request_id)
    with (workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
          workflow_common.lock(workflow_runtime.registry(c).with_suffix(".lock"), shared=True)):
        code = "import json; from autofusex.execution.environment import collect; print(json.dumps(collect()))"
        package = workflow_runtime.af_identity(json.loads(workflow_common.capture(
            [workflow_runtime.python(c), "-c", code], env=workflow_runtime.environment(c)).splitlines()[-1]))
        # The same group, lease and installed binary reuse a frozen POST; new binaries
        # get their own receipt without treating an old deployment as the runtime.
        key = workflow_common.digest({"lease": lease, "package": package})
        directory = Path(c["remote"]["state_dir"]) / "verification" / request_id
        path = directory / (key + ".json")
        with workflow_common.lock(path.with_suffix(".lock")):
            state = workflow_common.read(path) if path.exists() else {}
            if state.get("state") != "submitted":
                state = submit_issue_round(c, path, state, lease, package)
            return dict(state, request_id=request_id, package=package)
