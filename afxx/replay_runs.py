"""Local replay groups; execution records remain isolated and immutable per run."""

from datetime import datetime, timezone
from pathlib import Path
import uuid
import subprocess

from afxx import common, jobs


def _path(c, request_id):
    common.validate_job_id(request_id)
    return Path(c["local"]["state_dir"]) / "replay-groups" / (common.digest(request_id) + ".json")


def _context(c):
    return {"environment": c["connection"]["environment"], "state_dir": c["remote"]["state_dir"]}


def _load(c, request_id):
    path = _path(c, request_id)
    common.require_code(path.exists(), "REPLAY_NOT_FOUND", "未找到 replay 分组：" + request_id)
    value = common.read(path)
    common.require_code(value["context"] == _context(c), "CONFIG_CONFLICT",
                        "replay 分组的远端环境或 state_dir 与当前配置不同")
    return value


def _update(c, request_id, run, **fields):
    path = _path(c, request_id)
    with common.lock(path.with_suffix(".lock")):
        group = _load(c, request_id)
        group["runs"][run - 1].update(fields)
        common.write(path, group)


def submit(c, request, request_id, dry_run=False):
    from afxx.afx.fuzz_replay import validate_options
    validate_options(c, request)
    path = _path(c, request_id)
    with common.lock(path.with_suffix(".lock")):
        group = _load(c, request_id) if path.exists() else {
            "request_id": request_id, "context": _context(c), "runs": []}
        run = len(group["runs"]) + 1
        execution = uuid.uuid4().hex
        group["runs"].append({"run": run, "execution": execution,
                              "created_at": datetime.now(timezone.utc).isoformat(),
                              "seeds": request["seeds"], "dry_run": dry_run, "state": "submitting"})
        common.write(path, group)
    # Persist allocation before any upload; a lost connection remains discoverable.
    try:
        data = jobs.submit(c, dict(request, request_id=request_id, run=run), execution, dry_run)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = {"code": getattr(error, "code", "SUBMISSION_FAILED"), "message": str(error)}
        _update(c, request_id, run, state="submission_unknown", submission_error=detail)
        raise common.WorkflowError(detail["code"], detail["message"],
                                   {"request_id": request_id, "run": run, "state": "submission_unknown"}) from error
    state = data.get("state", "queued")
    _update(c, request_id, run, state=state)
    return {"request_id": request_id, "run": run, "state": state,
            "dry_run": dry_run, "local": data.get("local"), "remote": data.get("remote")}


def _select(c, request_id, run):
    group = _load(c, request_id)
    number = len(group["runs"]) if run is None else run
    common.require_code(type(number) is int and 1 <= number <= len(group["runs"]),
                        "REPLAY_NOT_FOUND", "未找到 replay run：" + str(number))
    return group["runs"][number - 1]


def _view(c, entry, full=False, download=True):
    out = {key: value for key, value in entry.items() if key != "execution"}
    if entry["dry_run"]:
        return out
    raw = jobs.query_job(c, entry["execution"])
    out["state"] = raw["state"]
    for key in ("phase", "error", "error_code", "log"):
        if key in raw:
            out[key] = raw[key]
    report = dict(raw.get("result.json", {}))
    for key in ("job", "deployment"):
        report.pop(key, None)
    out["passed"] = report.get("passed")
    if full:
        out["result"] = report
        if "execution_environments" in report:
            out["environment"] = report.get("execution_environments", {})
            out["submission_environment"] = raw.get("environment.json", {})
        else:
            out["environment"] = raw.get("environment.json", {})
        # Preserve the existing evidence download behavior for terminal replay.
        if download and raw["state"] in ("success", "failed"):
            localized = jobs.localize_replay(c, entry["execution"], dict(raw, task_type="replay"))
            out["evidence_path"] = localized.get("evidence_path")
    return out


def list_runs(c, request_id):
    return {"request_id": request_id, "runs": [_view(c, entry) for entry in _load(c, request_id)["runs"]]}


def show(c, request_id, run=None, *, download=True):
    return {"request_id": request_id, **_view(c, _select(c, request_id, run), full=True, download=download)}


def evidence(c, request_id, run, action, **options):
    """Resolve once so pagination can remain pinned to the returned run."""
    entry = _select(c, request_id, run)
    common.require_code(not entry["dry_run"], "EVIDENCE_UNAVAILABLE",
                        "dry-run replay has no remote evidence")
    value = jobs.query_evidence(c, entry["execution"], action, **options)
    return {**value, "request_id": request_id, "run": entry["run"]}


def _rows(report):
    rows = {}
    for attempt in report.get("attempts", []):
        seed = attempt.get("seed")
        backend = attempt.get("backend")
        for shape in attempt.get("shape_results", []) or [{}]:
            key = (seed, backend, shape.get("shape_index"))
            rows.setdefault(key, []).append({"status": attempt.get("status"),
                                            "correctness": shape.get("correctness"),
                                            "error_stage": attempt.get("error_stage"),
                                            "error_summary": attempt.get("error_summary")})
    return rows


def compare(c, request_id, before, after):
    left = show(c, request_id, before, download=False)
    right = show(c, request_id, after, download=False)
    common.require_code(all(item["state"] in ("success", "failed") for item in (left, right)),
                        "REPLAY_NOT_FINISHED", "只能对比已结束的 replay")
    a, b = _rows(left.get("result", {})), _rows(right.get("result", {}))
    differences = []
    for key in sorted(a.keys() | b.keys(), key=str):
        old, new = a.get(key, []), b.get(key, [])
        if key[0] is None or key[1] is None or key[2] is None or len(old) > 1 or len(new) > 1:
            state = "unmatched"
        elif not old:
            state = "added"
        elif not new:
            state = "missing"
        else:
            state = "unchanged" if old == new else "changed"
        differences.append({"seed": key[0], "backend": key[1], "shape_index": key[2],
                            "comparison": state, "before": old, "after": new})
    return {"request_id": request_id, "runs": [before, after],
            "environments": [left.get("environment"), right.get("environment")],
            "states": [left["state"], right["state"]],
            "passed": [left.get("passed"), right.get("passed")], "comparisons": differences}
