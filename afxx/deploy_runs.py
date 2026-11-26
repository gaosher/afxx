"""Deployment history; runs describe past installations, never select a runtime."""
from afxx import jobs, run_groups
from afxx.job_view import status_view


def submit(c, request, request_id, dry_run=False):
    return run_groups.submit(c, request, request_id, dry_run, kind="deploy")


def view(c, entry, full=False):
    out = {key: value for key, value in entry.items() if key not in ("execution", "seeds")}
    if entry["dry_run"]:
        return out
    raw = jobs.query_job(c, entry["execution"])
    out.update({key: raw[key] for key in ("state", "phase", "error", "error_code", "log") if key in raw})
    summary = status_view(raw)
    for key in ("available_details", "client_coordination", "installation_state", "exit_code", "artifact"):
        if key in summary:
            out[key] = summary[key]
    checks = raw.get("environment.json", {}).get("dependencies")
    if checks is not None:
        failed = [check for check in checks.get("checks", []) if check.get("passed") is False]
        out["dependencies"] = {"passed": checks.get("passed"), "failed_count": len(failed),
                               "failed_checks": [str(check.get("name", ""))[:128] for check in failed[:20]]}
    warnings = raw.get("environment.json", {}).get("errors", [])
    if warnings:
        out["warnings"] = [{"component": str(item.get("component", ""))[:128],
                            "message": str(item.get("error", ""))[:512]} for item in warnings[:20]]
        out["warning_count"] = len(warnings)
    report = raw.get("result.json", {})
    for key in ("package", "package_sha256"):
        if key in report:
            out[key] = report[key]
    if full:
        out["result"] = raw.get("result.json", {})
        out["environment"] = raw.get("environment.json", {})
    return out


def list_runs(c, request_id):
    return {"request_id": request_id, "runs": [view(c, entry) for entry in run_groups.load(c, request_id, "deploy")["runs"]]}


def show(c, request_id, run=None, full=False):
    return {"request_id": request_id, **view(c, run_groups.select(c, request_id, run, "deploy"), full)}
