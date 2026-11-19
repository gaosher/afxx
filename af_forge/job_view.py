# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Public job views; persisted reports remain complete and unchanged."""


from af_forge.afx.replay import failure_index


def status_view(data, full=False, attempt_id=None, fields=None):
    names = ("report.json", "python-dependencies.json", "deployment.json", "platform.json", "result.json")
    task_keys = ("job_id", "state", "phase", "pid", "exit_code", "error_code", "error", "log", "artifact", "snapshot_cleanup")
    out = {key: data[key] for key in task_keys if key in data}
    if "job" in data:
        out["job_id"] = data["job"]
    report = data.get("report.json", {})
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
        if "report.json" in details:
            value = details["report.json"]
            if "resources" in value:
                value["resources"] = {k: v for k, v in value["resources"].items() if k != "results_dir"}
            if "deployment" in value:
                value["deployment_job_id"] = value.pop("deployment")
        out["details"] = details
        return out
    if report:
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
    checks = data.get("python-dependencies.json")
    if checks is not None:
        out["dependencies"] = {"passed": checks.get("passed"),
                               "failed_checks": [check for check in checks.get("checks", [])
                                                 if check.get("passed") is False]}
    return out
