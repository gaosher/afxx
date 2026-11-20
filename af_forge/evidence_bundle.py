"""Download complete terminal replay evidence into a private local /tmp cache."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from af_forge import common
from af_forge.afx.replay import failure_index


def manifest(job, fallback_results):
    """Remote inventory. Include full regular files, never follow symlinks."""
    import hashlib
    import json
    import os
    from pathlib import Path
    job = Path(job)
    if job.is_symlink():
        raise ValueError("symlink job directory")
    status = json.loads((job / "status.json").read_text())
    request = json.loads((job / "request.json").read_text())
    if request.get("command") != "replay" or status.get("state") not in {"success", "failed"}:
        raise ValueError("evidence requires a terminal replay job")
    report = json.loads((job / "report.json").read_text()) if (job / "report.json").exists() else {}
    results = Path(report.get("results_dir") or fallback_results)
    roots = {"job": job, "results": results}
    files, skipped = [], []
    def add(path, group):
        if path.is_symlink():
            skipped.append({"path": str(path), "reason": "symlink"})
            return
        if not path.is_file():
            return
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1048576), b""):
                h.update(block)
        files.append({"remote_path": str(path), "group": group,
                      "relative_path": str(path.relative_to(roots[group])),
                      "size_bytes": path.stat().st_size, "sha256": h.hexdigest()})
    for name in ("report.json", "status.json", "request.json", "python-dependencies.json"):
        add(job / name, "job")
    for path in sorted(job.glob("*.log")):
        add(path, "job")
    scans = [("results", results)]
    scans += [("job", path) for path in sorted(job.iterdir())
              if path.name == "torch_compile_debug" or path.name.startswith("ascgen_dump_pid")]
    for group, root in scans:
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            raise ValueError("symlink diagnostic root: " + str(root))
        if not root.exists():
            continue
        def failed(error):
            raise error
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
            for name in list(dirs):
                if (Path(directory) / name).is_symlink():
                    skipped.append({"path": str(Path(directory) / name), "reason": "symlink"})
                    dirs.remove(name)
            dirs.sort()
            for name in sorted(names):
                path = Path(directory) / name
                if name == "sync.json":
                    skipped.append({"path": str(path), "reason": "upload_credentials"})
                    continue
                add(path, group)
    return {"state": "complete", "job_id": job.name, "remote_state": status["state"],
            "roots": {k: str(v) for k, v in roots.items()}, "files": files, "skipped": skipped}


def valid_cache(directory):
    try:
        if directory.is_symlink():
            return False
        value = json.loads((directory / "evidence.json").read_text())
        if value["state"] != "complete":
            return False
        for row in value["files"]:
            relative = Path(row["group"]) / row["relative_path"]
            if relative.is_absolute() or ".." in relative.parts:
                return False
            path = directory / relative
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if directory in parent.parents):
                return False
            if path.stat().st_size != row["size_bytes"] or common.sha(path) != row["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def cache_root():
    return Path("/tmp") / ("af-forge-evidence-" + str(os.getuid()))


def download(c, job_id, remote):
    import inspect
    root = cache_root()
    root.mkdir(mode=0o700, exist_ok=True)
    common.require(not root.is_symlink() and root.stat().st_uid == os.getuid()
                   and root.stat().st_mode & 0o077 == 0, "unsafe evidence cache directory")
    remote_job = str(Path(c["remote"]["state_dir"]) / job_id)
    identity = common.digest([c["connection"]["environment"], remote_job])[:16]
    destination = root / (job_id + "-" + identity)
    with common.lock(destination.with_suffix(".lock")):
        if valid_cache(destination):
            return destination
        common.require_code(shutil.which("npursync"), "DEPENDENCY_MISSING", "下载 evidence 需要 npursync；请安装 npussh 并加入 PATH")
        code = inspect.getsource(manifest) + "\n" + """
import json,sys
try:
 value=manifest(sys.argv[1],sys.argv[2])
 print(json.dumps({'state':'success','manifest':value}))
except (OSError, ValueError) as error:
 print(json.dumps({'state':'failed','error':str(error)}))
"""
        response = remote(c, ["python3", "-c", code, remote_job,
                              str(Path(c["replay"]["results_dir"]) / job_id)])
        common.require_code(response["state"] == "success", "EVIDENCE_DOWNLOAD_FAILED", response.get("error", "cannot list evidence"))
        value = response["manifest"]
        staging = Path(tempfile.mkdtemp(prefix=job_id + ".partial-", dir=root))
        try:
            for group in ("job", "results"):
                rows = [row for row in value["files"] if row["group"] == group]
                target = staging / group
                target.mkdir(mode=0o700)
                if not rows:
                    continue
                for row in rows:
                    path = Path(row["relative_path"])
                    common.require(not path.is_absolute() and ".." not in path.parts, "unsafe evidence path")
                listing = staging / (group + ".files")
                listing.write_bytes(b"".join(row["relative_path"].encode() + b"\0" for row in rows))
                common.run(["npursync", "-rt", "--no-links", "--from0", "--files-from=" + str(listing),
                            "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=",
                            c["connection"]["environment"] + ":" + value["roots"][group].rstrip("/") + "/", str(target) + "/"],
                           stdout=__import__('subprocess').DEVNULL, stderr=__import__('sys').stderr)
                listing.unlink()
            for row in value["files"]:
                path = staging / row["group"] / row["relative_path"]
                common.require(path.is_file() and not path.is_symlink()
                               and path.stat().st_size == row["size_bytes"] and common.sha(path) == row["sha256"],
                               "evidence changed or incomplete: " + row["remote_path"])
                row["local_path"] = str(destination / row["group"] / row["relative_path"])
            common.write(staging / "evidence.json", value)
            if destination.exists():
                destination.rename(destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex))
            staging.rename(destination)
            return destination
        except Exception as error:
            raise common.WorkflowError("EVIDENCE_DOWNLOAD_FAILED", "证据下载未完成；重查同一 job 重试",
                                       {"job_id": job_id, "remote_state": value["remote_state"], "partial_path": str(staging), "reason": str(error)}) from error


def terminal_result(directory, data):
    report_path = directory / "job" / "report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    attempts = report.get("attempts", [])
    by_id = {item.get("id"): item for item in attempts}
    failed = failure_index(attempts)
    failures = []
    for entry in failed[:20]:
        item = by_id[entry["attempt_id"]]
        shapes = [shape for shape in item.get("shape_results", []) if shape.get("shape_index") in entry["shape_indices"]]
        first = shapes[0] if shapes else {}
        failures.append(dict(entry, seed=item.get("seed_short") or item.get("seed"), backend=item.get("backend"),
                             stage=item.get("error_stage") or first.get("error_stage"),
                             error=str(item.get("error_summary") or first.get("error_summary") or "")[:512]))
    if data["state"] == "failed" and not failures:
        failures.append({"stage": data.get("phase"), "error": str(data.get("error") or report.get("error") or "replay failed")[:512]})
    result = {"job_id": data["job_id"], "state": data["state"], "evidence_path": str(directory), "failures": failures}
    if len(failed) > 20:
        result.update(failure_count=len(failed), failures_truncated=True)
    return result
