"""Deployment history; runs describe past installations, never select a runtime."""
from afxx import jobs, run_groups


def submit(c, request, request_id, dry_run=False):
    return run_groups.submit(c, request, request_id, dry_run, kind="deploy")


def view(c, entry, full=False):
    out = {key: value for key, value in entry.items() if key not in ("execution", "seeds")}
    if entry["dry_run"]:
        return out
    raw = jobs.query_job(c, entry["execution"])
    out.update({key: raw[key] for key in ("state", "phase", "error", "error_code", "log") if key in raw})
    if full:
        out["result"] = raw.get("result.json", {})
        out["environment"] = raw.get("environment.json", {})
    return out


def list_runs(c, request_id):
    return {"request_id": request_id, "runs": [view(c, entry) for entry in run_groups.load(c, request_id, "deploy")["runs"]]}


def show(c, request_id, run=None):
    return {"request_id": request_id, **view(c, run_groups.select(c, request_id, run, "deploy"), True)}
