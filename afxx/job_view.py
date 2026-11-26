"""Public job views; persisted reports remain complete and unchanged."""


from afxx.afx.replay import failure_index


def status_view(data, full=False, attempt_id=None, fields=None):
    names = ("result.json", "environment.json", "platform.json")
    task_keys = ("job_id", "task_type", "state", "phase", "pid", "exit_code", "error_code", "error", "log", "artifact", "snapshot_cleanup")
    out = {key: data[key] for key in task_keys if key in data}
    if "job" in data:
        out["job_id"] = data["job"]
    report = data.get("result.json", {})
    attempts = report.get("attempts", [])
    if attempt_id is not None:
        selected = [item for item in attempts if item.get("id") == attempt_id]
        if len(selected) != 1:
            raise ValueError("attempt not found in this job: " + attempt_id)
        item = selected[0]
        names = fields or ["id", "seed_short", "backend", "status", "error_stage", "error_summary"]
        result = {}
        truncated = []
        for name in names:
            if fields is None and name not in item:
                continue
            value = item
            for part in name.split("."):
                if not isinstance(value, dict) or part not in value:
                    raise ValueError("attempt field not found: " + name)
                value = value[part]
            if fields is None and isinstance(value, str) and len(value) > 512:
                value = value[:512]
                truncated.append(name)
            result[name] = value
        if truncated:
            out["attempt_truncated_fields"] = truncated
        out["attempt"] = result
        return out
    out["available_details"] = [name for name in names if name in data]
    if full:
        details = {name: dict(data[name]) for name in names if name in data}
        for value in details.values():
            for key in ("job", "state", "exit_code", "error"):
                if key in value and value[key] == data.get(key):
                    value.pop(key)
            if "job" in value:
                value["job_id"] = value.pop("job")
        if "result.json" in details:
            value = details["result.json"]
            if "resources" in value:
                value["resources"] = {k: v for k, v in value["resources"].items() if k != "results_dir"}
            if "deployment" in value:
                value["deployment_job_id"] = value.pop("deployment")
        out["details"] = details
        return out
    if "attempts" in report or data.get("task_type") == "replay":
        failed = failure_index(attempts)
        evidence = report.get("evidence", {})
        out["replay"] = {
            "passed": report.get("passed"), "errors": report.get("errors", [])[:20],
            "errors_truncated": len(report.get("errors", [])) > 20,
            "deployment_job_id": report.get("deployment"), "results_dir": report.get("results_dir"),
            "attempt_count": len(attempts), "failed_attempt_count": len(failed),
            "failures": failed[:20], "failures_truncated": len(failed) > 20,
            "evidence": {"file_count": len(evidence.get("files", [])),
                         "truncated": evidence.get("truncated", False),
                         "collection_error_count": len(evidence.get("errors", []))},
        }
    if data.get("task_type") == "fuzz-run":
        out["fuzz"] = {key: report[key] for key in ("results_dir", "database", "exit_code") if key in report}
    if data.get("task_type") == "update":
        installed = report.get("installed", {})
        cann = installed.get("cann", {})
        out["update"] = {"packages": installed.get("packages", {}),
                         "cann": {k: cann[k] for k in ("version", "date", "release") if k in cann},
                         "components": report.get("components", [])}
        if "rollback" in report:
            out["update"]["rollback"] = report["rollback"]
    if "client_coordination" in report:
        coordination = report["client_coordination"]
        out["client_coordination"] = {k: coordination[k] for k in
            ("state", "restore_mode", "execution_environment_state", "error", "reason") if k in coordination}
        out["installation_state"] = report.get("state", "not_verified")
    checks = data.get("environment.json", {}).get("dependencies")
    if checks is not None:
        out["dependencies"] = {"passed": checks.get("passed"),
                               "failed_checks": [check for check in checks.get("checks", [])
                                                 if check.get("passed") is False]}
    return out
