# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Read-only comparison of configuration, environment records and installed packages."""

from af_forge import common as workflow_common
from af_forge.environment import runtime as workflow_runtime

import hashlib
import json
from pathlib import Path
import re


class EnvironmentMismatch(ValueError):
    """Configuration or recorded environment differs from the current environment."""


def record(path):
    value = workflow_common.read(path) if path.is_file() else {}
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object, actual {type(value).__name__}")
    return value


def version_info(path):
    if not path.is_file():
        return {}
    return dict(
        line.split("=", 1) for line in path.read_text().splitlines() if "=" in line
    )


def check(c):
    differences = []

    def compare(field, expected, actual):
        if expected != actual:
            differences.append(f"{field}: expected={expected!r}, actual={actual!r}")

    def finish():
        if differences:
            raise EnvironmentMismatch("Environment mismatch: " + "; ".join(differences))

    venv = Path(c["runtime"]["venv_dir"])
    root = Path(c["runtime"]["install_root"])
    marker = (
        record(workflow_runtime.env_manifest_path(c)) if c.get("python_env") else {}
    )
    compare("environment state", "ready", marker.get("state"))
    compare("environment manager", "uv", marker.get("manager"))
    finish()
    baseline = marker
    for field in ("venv_dir", "install_root"):
        compare(field, c["runtime"][field], baseline.get("runtime", {}).get(field))
    compare(
        "connection.environment",
        c["connection"]["environment"],
        baseline.get("connection", {}).get("environment"),
    )
    p = c.get("python_env", {})
    compare("python_env configuration", True, bool(p))
    finish()

    compare(
        "stable requirements", p["stable_requirements"], marker["stable_requirements"]
    )
    lock = Path(marker["lock"]["path"])
    compare(
        "python lock sha256",
        marker["lock"]["sha256"],
        workflow_common.sha(lock) if lock.is_file() else None,
    )
    artifacts = marker["artifacts"]
    daily = p["torch_npu"]
    for component in ("afx", "torch_npu"):
        item = artifacts[component]
        wheel = Path(item["path"])
        origin = item["source"]
        compare(
            component + " wheel sha256",
            item["sha256"],
            workflow_common.sha(wheel) if wheel.is_file() else None,
        )
        if component == "afx":
            if p["afx"].get("version") != "latest":
                compare("afx.version", p["afx"].get("version"), origin.get("version"))
            if p["afx"].get("version") not in (None, "latest"):
                compare(
                    "afx wheel version",
                    p["afx"]["version"],
                    item["metadata"]["version"],
                )
            if p["afx"]["wheel"] != item["path"]:
                compare("afx.wheel", p["afx"]["wheel"], origin.get("wheel"))
            if p["afx"].get("sha256"):
                compare("afx.sha256", p["afx"]["sha256"], item["sha256"])
            source = p["afx"]["wheel"]
            if source and Path(source).is_absolute():
                compare(
                    "afx configured file sha256",
                    item["sha256"],
                    workflow_common.sha(source) if Path(source).is_file() else None,
                )
        else:
            selected = origin
            compare("torch_npu.series", daily["version"], origin.get("version"))
            compare(
                "torch_npu selected date recorded", True, bool(selected.get("date"))
            )
            if daily["date"] != "latest":
                compare("torch_npu.date", daily["date"], selected.get("date"))
            compare(
                "torch_npu.url_template",
                daily["url_template"],
                origin.get("url_template"),
            )
            if daily.get("sha256"):
                compare("torch_npu.archive sha256", daily["sha256"], origin["sha256"])

    cann = c.get("cann", {})
    receipt = marker["cann"]
    info = version_info(root / "cann/compiler/version.info")
    compare(
        "CANN environment script exists", True, (root / "cann/set_env.sh").is_file()
    )
    if receipt["packages"]:
        live = record(root / ".af-cann-bootstrap.json")
        compare("CANN installation state", "ready", live.get("state"))
        compare(
            "CANN package hashes",
            receipt["artifacts"],
            [
                {"name": Path(item["path"]).name, "sha256": item["sha256"]}
                for item in live.get("artifacts", [])
            ],
        )
    version = receipt["version"]
    release = receipt["release"]
    actual_release = info.get("timestamp", "").replace("_", "")
    compare("CANN version", version or "recorded Toolkit version", info.get("Version"))
    compare("CANN release", release or "recorded Toolkit release", actual_release)
    if cann.get("date", "latest") != "latest":
        compare("cann.date", cann["date"], actual_release[:8])
    init_cann = baseline["cann_config"]
    if receipt.get("packages"):
        ops = next((name for kind, name in receipt["packages"] if kind == "ops"), "")
        family = re.fullmatch(r"Ascend-cann-(.+?)-ops_.*", ops)
        configured_ops = cann.get("ops")
        if configured_ops in (None, "", "auto"):
            for device in receipt.get("hardware", []):
                compare(
                    "cann.ops mapping for " + device["identity"],
                    cann.get("ops_compatibility", {}).get(device["identity"]),
                    device["ops"],
                )
        elif configured_ops != "none":
            compare("cann.ops", configured_ops, family.group(1) if family else None)
        compare("CANN OPS requested", configured_ops != "none", bool(ops))
        if ops:
            ops_info = version_info(root / "cann/opp/version.info")
            compare("CANN OPS version", version, ops_info.get("Version"))
            compare(
                "CANN OPS release",
                release,
                ops_info.get("timestamp", "").replace("_", ""),
            )
        compare(
            "cann.nnal",
            cann.get("nnal", False),
            any(k == "nnal" for k, _ in receipt["packages"]),
        )
    else:
        for field in ("ops", "nnal", "ops_compatibility"):
            compare("cann." + field, cann.get(field), init_cann.get(field))

    script = """import importlib, importlib.metadata as md, json, platform, re, sys, sysconfig
from pathlib import Path
rows = {}
for dist, module in [('torch','torch'), ('torch-npu','torch_npu'), ('autofusex','autofusex')]:
 try:
  m = importlib.import_module(module)
  d = md.distribution(dist)
  rows[dist] = [d.version, str(m.__version__), str(Path(m.__file__).resolve()),
                json.loads(d.read_text('direct_url.json') or '{}').get('url')]
 except Exception as e:
  rows[dist] = [None, None, str(e), None]
paths = {sysconfig.get_path(key) for key in ('purelib', 'platlib')}
packages = {re.sub('[-_.]+', '-', d.metadata['Name']).lower(): d.version for d in md.distributions(path=paths)}
print(json.dumps([sys.prefix, 'Python ' + platform.python_version(), packages, rows]))"""
    exe = venv / "bin/python"
    compare("venv interpreter exists", True, exe.is_file())
    if not exe.is_file() or not (root / "cann/set_env.sh").is_file():
        finish()
    prefix, interpreter, packages, rows = json.loads(
        workflow_common.capture(
            [str(exe), "-c", script], env=workflow_runtime.environment(c), timeout=120
        ).splitlines()[-1]
    )
    compare("active venv", venv.resolve(), Path(prefix).resolve())
    compare("Python version", marker["python"], interpreter)
    for name in sorted(marker["packages"].keys() | packages.keys()):
        compare(name + " version", marker["packages"].get(name), packages.get(name))
    torch = next(
        r.split("==", 1)[1] for r in p["stable_requirements"] if r.startswith("torch==")
    )
    for dist, (installed, imported, path, url) in rows.items():
        expected = marker["packages"].get(dist)
        compare(dist + " installed version", expected, installed)
        compare(dist + " imported version", expected, imported)
        if not installed or not workflow_common.inside(Path(path), venv.resolve()):
            differences.append(f"{dist} import: expected=under {venv}, actual={path}")
        if dist == "torch":
            compare("torch config", torch, installed)
        else:
            item = artifacts["afx" if dist == "autofusex" else "torch_npu"]
            compare(dist + " wheel version", item["metadata"]["version"], installed)
            compare(dist + " installed origin", Path(item["path"]).as_uri(), url)
    if "environment_lock" in c:
        pins = c["environment_lock"]
        compare("frozen lock sha256", pins["pylock_sha256"], marker["lock"]["sha256"])
        compare(
            "embedded lock sha256",
            pins["pylock_sha256"],
            hashlib.sha256(pins["pylock"].encode()).hexdigest(),
        )
        compare("frozen Python version", pins["python"], interpreter)
        compare("frozen packages", pins["packages"], packages)
        compare("frozen CANN", pins["cann"], receipt)
        compare(
            "frozen wheels",
            pins["wheels"],
            {k: item["sha256"] for k, item in artifacts.items()},
        )
    finish()
    return marker
