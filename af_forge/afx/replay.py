# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Current AFX seed, direct replay and result-query boundary."""

import json
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
