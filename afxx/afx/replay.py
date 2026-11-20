"""Current AFX seed, direct replay and result-query boundary."""

import json
import os
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def requested_seeds(seed, seeds_file):
    require(seed or seeds_file, "replay requires --seed or --seeds-file")
    values = (
        [seed]
        if seed
        else [s.strip() for s in Path(seeds_file).read_text().splitlines() if s.strip()]
    )
    require(
        values and all(re.fullmatch(r"[A-Za-z0-9]{1,128}", s) for s in values),
        "invalid seed strings",
    )
    return values


def seed_command(interpreter, seeds):
    code = """import json,sys
from autofusex.fuzz.client.contracts import Seed
from autofusex.fuzz.client.generation.sampling import decode
out=[]
for value in json.loads(sys.argv[1]):
 s=Seed.parse(value)
 out.append([str(s),decode(s)[0].dynamic_variants if s.dynamic else 1])
print(json.dumps(out))
"""
    return [interpreter, "-c", code, json.dumps(seeds)]


def normalized_seeds(output):
    lines = output.splitlines()
    require(lines, "empty debugger seed response")
    values = json.loads(lines[-1])
    require(
        isinstance(values, list)
        and values
        and all(
            isinstance(row, list)
            and len(row) == 2
            and isinstance(row[0], str)
            and re.fullmatch(r"[A-Za-z0-9]{1,128}", row[0])
            and type(row[1]) is int
            and 1 <= row[1] <= 8
            for row in values
        ),
        "invalid debugger seed response",
    )
    require(len({seed for seed, _ in values}) == len(values), "duplicate seeds")
    return values


def replay_environment(results, dump):
    env = {
        "AUTOFUSEX_HOME": str(results),
        "TORCHINDUCTOR_CACHE_DIR": str(results / "cache"),
        "AUTOFUSE_DFX_FLAGS": "--mlir_codegen_config=mlir;",
    }
    if dump:
        graph_dir = results / "dump" / "ge"
        graph_dir.mkdir(parents=True, exist_ok=True)
        env.update({
            "TORCH_COMPILE_DEBUG": "1",
            "DUMP_GE_GRAPH": "1",
            "DUMP_GRAPH_LEVEL": "1",
            "DUMP_GRAPH_PATH": str(graph_dir),
            "ASCEND_GLOBAL_LOG_LEVEL": "0",
            "ASCEND_SLOG_PRINT_TO_STDOUT": "1",
        })
        env["AUTOFUSE_DFX_FLAGS"] += (
            "--codegen_compile_debug=true;--debug_dir=" + str(results / "dump") + ";"
        )
    return env


def replay_command(interpreter, options, normalized, results, batch):
    seeds = [seed for seed, _ in normalized]
    common = [
        "--save",
        "--database",
        str(results / "results.sqlite"),
        "--scheduling",
        "staged",
        "--compile-threads",
        str(options["compile_threads"]),
    ]
    if batch:
        args = [
            "run",
            *seeds,
            "--data-dir",
            str(results),
            "--fg",
            "--fixed-resources",
            "--workers",
            str(options["workers"]),
            "--timeout",
            str(options["batch_timeout_seconds"]),
        ]
        for device in options["devices"]:
            args.extend(["--device", device])
    else:
        require(len(seeds) == 1, "single replay requires exactly one seed")
        args = ["replay", seeds[0], "--device", options["devices"][0]]
    return [interpreter, "-m", "autofusex", "fuzz", *args, *common]


def sync_command(interpreter, results):
    return [
        interpreter,
        "-m",
        "autofusex",
        "fuzz",
        "maintenance",
        "sync",
        "start",
        "--data-dir",
        str(results),
    ]


def read_results(interpreter, database, capture):
    """Use AFX's direct-job result CLI, including pagination and attempt details."""
    require(database.is_file(), "missing results database")
    command = [
        interpreter,
        "-m",
        "autofusex",
        "fuzz",
        "maintenance",
        "results",
        str(database),
    ]
    attempts, after = [], 0
    while True:
        page = json.loads(capture([*command, "--limit", "200", "--after", str(after)]))
        require(
            isinstance(page, dict) and isinstance(page.get("items"), list),
            "invalid AFX results page",
        )
        for row in page["items"]:
            detail = json.loads(capture([*command, "--attempt", row["id"]]))
            require(detail["id"] == row["id"], "AFX attempt identity mismatch")
            attempts.append(detail)
        cursor = page["next_cursor"]
        if cursor is None:
            return attempts
        require(
            type(cursor) is int and cursor > after and page["items"],
            "invalid AFX results cursor",
        )
        after = cursor


def summarize(attempts, normalized):
    expected = {
        (seed, backend): count
        for seed, count in normalized
        for backend in ("cpu", "af-mlir")
    }
    actual = {}
    for attempt in attempts:
        key = (attempt["seed"], attempt["backend"])
        require(key not in actual, "duplicate result: " + str(key))
        actual[key] = attempt
    errors = []
    if set(actual) != set(expected):
        errors.append("seed/backend coverage mismatch")
    for key, count in expected.items():
        result = actual.get(key)
        # CPU supplies the reference; AFX does not compare it against itself.
        allowed_correctness = {"pass", "not_checked"} if key[1] == "cpu" else {"pass"}
        if (
            not result
            or result["status"] != "ok"
            or sorted(s["shape_index"] for s in result["shape_results"])
            != list(range(count))
            or any(
                s["correctness"] not in allowed_correctness
                for s in result["shape_results"]
            )
        ):
            errors.append("failed or incomplete: " + str(key))
    return {"passed": not errors, "errors": errors, "attempts": attempts}


def device_command(interpreter):
    return [
        interpreter,
        "-c",
        "import json,torch,torch_npu; print(json.dumps(torch.npu.device_count()))",
    ]


def visible_device_count(output):
    lines = output.splitlines()
    require(lines, "NPU device probe returned no output")
    count = json.loads(lines[-1])
    require(
        type(count) is int and count > 0,
        "no visible NPU devices; check driver and device visibility",
    )
    return count


def collect_evidence(job, results, dump, offset=0):
    """Return bounded, local diagnostic evidence without following symlinks."""
    roots = {
        "af_dump": results / "dump",
        "torch_debug": job / "torch_compile_debug",
        "inductor_cache": results / "cache",
    }
    evidence = {"dump_enabled": dump, "files": [], "directories": {},
                "errors": [], "truncated": False}
    remaining = 65536
    seen = 0
    text_suffixes = {".log", ".txt", ".mlir", ".py", ".cpp", ".cc", ".h", ".json"}

    def add(path, category):
        nonlocal remaining, seen
        if evidence["truncated"] or path.is_symlink() or not path.is_file():
            return
        seen += 1
        if seen <= offset:
            return
        if len(evidence["files"]) >= 128:
            evidence["truncated"] = True
            return
        try:
            size = path.stat().st_size
            row = {"path": str(path.absolute()), "category": category, "size_bytes": size}
            if path.suffix in text_suffixes:
                limit = min(2048, remaining)
                with path.open("rb") as stream:
                    excerpt_offset = max(0, size - limit) if path.suffix == ".log" else 0
                    stream.seek(excerpt_offset)
                    content = stream.read(limit)
                row.update(excerpt=content.decode("utf-8", errors="replace"),
                           excerpt_offset=excerpt_offset, excerpt_truncated=size > len(content))
                remaining -= len(content)
            evidence["files"].append(row)
        except OSError as error:
            evidence["errors"].append({"path": str(path), "error": str(error)})

    # Logs first so a large compiler dump cannot crowd out the failure message.
    add(job / "job.log", "job_log")
    add(results / "results.sqlite", "results_database")
    for category, root in {"af_dump": roots["af_dump"], "torch_debug": roots["torch_debug"],
                           "results": results}.items():
        info = {"path": str(root.absolute()), "exists": root.is_dir(), "files_returned": 0}
        evidence["directories"][category] = info
        if evidence["truncated"] or root.is_symlink() or not root.is_dir():
            continue
        before = len(evidence["files"])
        def onerror(error):
            evidence["errors"].append({"path": error.filename, "error": str(error)})
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink()
                             and not (category == "results" and Path(directory) == results and d == "dump"))
            for name in sorted(files):
                path = Path(directory) / name
                if category == "results":
                    # Only diagnostic files: never sync.json (may contain credentials).
                    if path.suffix not in text_suffixes or path.name == "sync.json":
                        continue
                    kind = "inductor_cache" if roots["inductor_cache"] in path.parents else "worker_log"
                else:
                    kind = category
                add(path, kind)
                if evidence["truncated"]:
                    break
            if evidence["truncated"]:
                break
        info["files_returned"] = len(evidence["files"]) - before
    evidence["next_offset"] = seen - 1 if evidence["truncated"] else None
    return evidence


def failure_index(attempts):
    """Compact failure references using AFX reference-backend semantics."""
    failed = []
    for attempt in attempts:
        allowed = {"pass", "not_checked"} if attempt.get("backend") == "cpu" else {"pass"}
        shapes = [shape.get("shape_index") for shape in attempt.get("shape_results", [])
                  if shape.get("correctness") not in allowed]
        if attempt.get("status") != "ok" or shapes:
            failed.append({"attempt_id": attempt.get("id"), "shape_indices": shapes})
    return failed


def associate_evidence(evidence, attempts):
    """Only exact file references in AFX diagnostics establish attempt ownership."""
    def references(value, path):
        if isinstance(value, dict):
            return any(references(item, path) for item in value.values())
        if isinstance(value, list):
            return any(references(item, path) for item in value)
        if isinstance(value, str):
            return re.search(r"(?<![\w/.-])" + re.escape(path) + r"(?![\w/.-])", value) is not None
        return False

    evidence["failures"] = failure_index(attempts)
    for row in evidence["files"]:
        row["associations"] = []
        row["association_state"] = "unassociated"
    for attempt in attempts:
        diagnostics = attempt.get("contexts", {}).get("execution", {}).get("diagnostics", {})
        if isinstance(diagnostics, str):
            try:
                diagnostics = json.loads(diagnostics)
            except ValueError:
                diagnostics = {"error": diagnostics}
        for row in evidence["files"]:
            path = Path(row["path"])
            roots = [Path(info["path"]) for info in evidence.get("directories", {}).values()]
            candidates = [path, *(parent for parent in path.parents
                                  if any(root in parent.parents for root in roots))]
            reference = next((str(candidate) for candidate in candidates
                              if references(diagnostics, str(candidate))), None)
            if reference:
                shapes = diagnostics.get("shapes", {}) if isinstance(diagnostics, dict) else {}
                indices = [key for key, value in shapes.items() if references(value, reference)] if isinstance(shapes, dict) else []
                row["associations"].append(dict(attempt_id=attempt.get("id"), shape_indices=indices,
                    reference_path=reference, basis="exact_path_in_afx_diagnostics" if reference == str(path)
                    else "explicit_directory_in_afx_diagnostics"))
                row["association_state"] = "referenced"
    return evidence


def read_evidence(job, path, offset, limit, expected_sha256=None):
    """Read one text artifact, confined to diagnostic roots of this job."""
    import hashlib
    report = json.loads((job / "result.json").read_text())
    results = Path(report["results_dir"])
    path = Path(path)
    if offset < 0 or not 1 <= limit <= 65536 or not path.is_absolute() or ".." in path.parts:
        raise ValueError("invalid path, byte offset or limit (1..65536)")
    roots = [job / "torch_compile_debug", results / "dump", results / "cache"]
    allowed = path == job / "job.log" or any(root in path.parents for root in roots)
    # Other result files are restricted to worker logs, not arbitrary JSON/config.
    allowed = allowed or (results in path.parents and path.suffix == ".log")
    if not allowed or path.name == "sync.json" or path.suffix not in {".log", ".txt", ".mlir", ".py", ".cpp", ".cc", ".h", ".json"}:
        raise ValueError("not a readable diagnostic text artifact of this job")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("symlink artifacts are not readable")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else None
        if digest is None:
            stream.seek(0)
            h = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1048576), b""):
                h.update(chunk)
            digest = h.hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise ValueError("artifact changed; restart reading from offset 0")
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(offset)
        data = stream.read(limit)
    if b"\x00" in data:
        raise ValueError("binary artifact cannot be read as text")
    return {"path": str(path), "sha256": digest, "size_bytes": size,
            "offset": offset, "bytes_read": len(data), "content": data.decode("utf-8", errors="replace"),
            "next_offset": offset + len(data) if offset + len(data) < size else None,
            "eof": offset + len(data) >= size}
