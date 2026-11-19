#!/usr/bin/env python3
# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""Workflow configuration loading, defaults and validation."""

import datetime
import json
import math
from pathlib import Path
import re
import shlex
import sys
import urllib.parse
from af_forge import common as workflow_common


FIELDS = {
    "connection": {"environment"},
    "local": {"source_dir", "state_dir"},
    "remote": {"source_dir", "state_dir"},
    "runtime": {"install_root", "venv_dir"},
    "build": {"llvm_build_dir", "lld_bin", "jobs"},
    "sync": {"include_ignored_paths", "exclude_paths"},
    "replay": {
        "results_dir",
        "devices",
        "workers",
        "compile_threads",
        "batch_timeout_seconds",
        "dump",
    },
    "upload": {"enabled", "sync_config_path"},
}


def read_config(path):
    path = Path(path)
    workflow_common.require(
        path.suffix.lower() in {".yaml", ".yml"},
        "用户配置仅支持 .yaml / .yml；请将配置转换为 YAML。",
    )
    try:
        import yaml
    except ImportError as error:
        raise workflow_common.WorkflowError(
            "DEPENDENCY_MISSING",
            "读取 YAML 配置需要 PyYAML；请为当前解释器安装依赖："
            + shlex.quote(sys.executable)
            + ' -m pip install "PyYAML>=6.0,<7"',
        ) from error

    class ConfigLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                workflow_common.require(
                    isinstance(key, str), "YAML configuration keys must be strings"
                )
                workflow_common.require(key not in result, "duplicate YAML key: " + key)
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    try:
        value = yaml.load(path.read_text(), Loader=ConfigLoader)
        # Reject timestamps, sets, binary values, recursive aliases and non-finite numbers:
        # user configs must freeze losslessly into JSON tasks.
        json.dumps(value, allow_nan=False)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        location = " at line " + str(mark.line + 1) if mark else ""
        raise ValueError(
            "invalid YAML configuration"
            + location
            + "; use plain mappings/lists/scalars, without custom tags or merge keys"
        ) from error
    except (TypeError, ValueError, RecursionError) as error:
        if isinstance(error, ValueError) and str(error).startswith(
            ("duplicate YAML key:", "YAML configuration keys")
        ):
            raise
        raise ValueError(
            "YAML configuration must contain JSON-compatible values; quote dates and versions"
        ) from error
    return value


STABLE_REQUIREMENTS = [
    "setuptools>=68,<81",
    "build>=1",
    "wheel",
    "packaging",
    "numpy",
    "sympy",
    "attrs>=23",
    "decorator>=5.1",
    "psutil>=5.9",
    "scipy>=1.7.3",
    "PyYAML",
    "jinja2",
    "ninja",
    "protobuf",
    "absl-py",
    "cloudpickle",
    "six",
    "ml-dtypes",
    "z3-solver==4.12.2",
]


def merge_config(defaults, supplied, path=""):
    workflow_common.require(
        isinstance(supplied, dict), "configuration section must be a mapping: " + path
    )
    workflow_common.require(
        not set(supplied) - set(defaults),
        "unknown configuration fields: "
        + path
        + str(sorted(set(supplied) - set(defaults))),
    )
    result = dict(defaults)
    for key, value in supplied.items():
        result[key] = (
            merge_config(defaults[key], value, path + key + ".")
            if isinstance(defaults[key], dict) and defaults[key]
            else value
        )
    return result


def normalize_config(raw):
    workflow_common.require(isinstance(raw, dict), "configuration must be a mapping")
    remote = raw.get("remote", {})
    local = raw.get("local", {})
    workflow_common.require(
        isinstance(remote, dict) and isinstance(local, dict),
        "local/remote must be mappings",
    )
    root = raw.get("workflow_root")
    workflow_common.require(
        "workflow_root" not in raw or "workspace" not in remote,
        "use workflow_root or remote.workspace, not both",
    )
    if "workflow_root" in raw:
        workflow_common.require(
            isinstance(root, str)
            and re.fullmatch(r"/[A-Za-z0-9_./+\-]+", root)
            and ".." not in Path(root).parts
            and len(Path(root).parts) > 2,
            "invalid workflow_root",
        )
    workspace = root if root is not None else remote.get("workspace")
    if workspace is not None:
        workflow_common.require(
            isinstance(workspace, str)
            and re.fullmatch(r"/[A-Za-z0-9_./+\-]+", workspace)
            and ".." not in Path(workspace).parts
            and len(Path(workspace).parts) > 2,
            "invalid remote.workspace",
        )

    def under(name):
        return str(Path(workspace) / name) if workspace else None

    defaults = {
        "workflow_root": root,
        "schema_version": 1,
        "connection": {"environment": None},
        "local": {
            "source_dir": None,
            "state_dir": str(
                Path.home() / ".local/state/graph-autofusion/dev_workflow/jobs"
            ),
        },
        "remote": {
            "workspace": workspace,
            "source_dir": under(
                "graph-autofusion" if root else "graph-autofusion-workflow"
            ),
            "state_dir": under("workflow-state"),
        },
        "runtime": {
            "install_root": under("Ascend") if root else None,
            "venv_dir": under("venv") if root else None,
        },
        "build": {"llvm_build_dir": None, "lld_bin": None, "jobs": 8},
        "sync": {"include_ignored_paths": [], "exclude_paths": []},
        "replay": {
            "results_dir": under("workflow-results"),
            "devices": "auto",
            "workers": "auto",
            "compile_threads": 4,
            "batch_timeout_seconds": 300,
            "dump": False,
        },
        "upload": {"enabled": False, "sync_config_path": under("sync.json")},
    }
    if "platform" in raw:
        defaults["platform"] = {
            "url": None,
            "agent_id": None,
            "token_file": None,
            "remote_token_file": None,
            "client_token_file": None,
        }
    if "python_env" in raw:
        workflow_common.require(
            isinstance(raw["python_env"], dict), "python_env must be a mapping"
        )
        workflow_common.require(
            not {"torch_version", "stable_requirements"} <= set(raw["python_env"]),
            "use torch_version or explicit stable_requirements, not both",
        )
        defaults["python_env"] = {
            "base_python": None,
            "python_dir": under("python"),
            "uv_bin": None,
            "state_dir": under("workflow-env-state"),
            "index_url": None,
            "torch_index_url": None,
            "torch_version": "2.10.0+cpu",
            "stable_requirements": None,
            "torch_npu": {
                "version": None,
                "date": "latest",
                "url_template": None,
                "sha256": None,
            },
            "afx": {"version": None, "wheel": None, "sha256": None},
        }
    if "cann" in raw:
        defaults["cann"] = {
            "install_root": None,
            "date": "latest",
            "auto_install": True,
            "repository_url": None,
            "ops": "auto",
            "nnal": False,
            "ops_compatibility": {},
            "driver_library_dirs": [],
        }
    if "environment_lock" in raw:
        defaults["environment_lock"] = {}
    c = merge_config(defaults, raw)
    if "python_env" in c:
        if c["python_env"]["python_dir"] is None:
            c["python_env"]["python_dir"] = str(Path(c["runtime"]["venv_dir"]).parent / "python")
        afx = c["python_env"]["afx"]
        if afx.get("version") is None and afx.get("wheel") is None:
            afx["version"] = "latest"
    c.pop("workflow_root")
    c["remote"].pop("workspace")

    def package_source(name):
        return workflow_common.read(
            Path(__file__).with_name("environment") / "package_sources.json"
        )[name]

    if "cann" in c:
        cann = c["cann"]
        install_root = cann.pop("install_root")
        if install_root is not None:
            legacy_root = raw.get("runtime", {}).get("install_root")
            workflow_common.require(
                legacy_root is None or legacy_root == install_root,
                "cann.install_root conflicts with runtime.install_root",
            )
            c["runtime"]["install_root"] = install_root
        date = cann["date"]
        if date != "latest":
            workflow_common.require(
                isinstance(date, str) and re.fullmatch(r"[0-9]{8}", date),
                "cann.date must be latest or YYYYMMDD",
            )
            try:
                datetime.datetime.strptime(date, "%Y%m%d")
            except ValueError as error:
                raise ValueError(
                    "invalid calendar date for cann.date: " + date
                ) from error
        mappings = cann["ops_compatibility"]
        workflow_common.require(
            isinstance(mappings, dict), "cann.ops_compatibility must be a mapping"
        )
        cann["ops_compatibility"] = {**package_source("ops_compatibility"), **mappings}
        if cann["repository_url"] is None:
            cann["repository_url"] = package_source("cann_repository")
    if "python_env" in c:
        version = c["python_env"].pop("torch_version")
        workflow_common.require(
            isinstance(version, str)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+[A-Za-z0-9.]+)?", version),
            "invalid python_env.torch_version",
        )
        if c["python_env"]["stable_requirements"] is None:
            c["python_env"]["stable_requirements"] = [
                "torch==" + version,
                *STABLE_REQUIREMENTS,
            ]
        requirements = c["python_env"]["stable_requirements"]
        workflow_common.require(
            isinstance(requirements, list), "stable_requirements must be a list"
        )
        pins = [
            re.fullmatch(r"torch==([0-9]+\.[0-9]+\.[0-9]+)(?:\+[A-Za-z0-9.]+)?", item)
            for item in requirements
            if isinstance(item, str) and item.startswith("torch==")
        ]
        workflow_common.require(
            len(pins) == 1 and pins[0], "pin exactly one torch==VERSION"
        )
        series = pins[0].group(1)
        daily = c["python_env"]["torch_npu"]
        workflow_common.require(
            isinstance(daily, dict) and daily.get("version") in (None, series),
            "torch_npu.version must match the pinned torch version " + series,
        )
        daily["version"] = series
        if daily["url_template"] is None:
            daily["url_template"] = package_source("torch_npu_daily")
        if c["python_env"]["torch_index_url"] is None and pins[0].string.endswith(
            "+cpu"
        ):
            c["python_env"]["torch_index_url"] = package_source("torch_cpu_index")
    return c


def config(path):
    return validate_config(normalize_config(read_config(path)))


def validate_config(c):
    workflow_common.require(isinstance(c, dict), "configuration must be a mapping")
    workflow_common.require(
        set(FIELDS) | {"schema_version"} <= set(c)
        and set(c)
        <= set(FIELDS)
        | {"schema_version", "python_env", "cann", "environment_lock", "platform"}
        and type(c["schema_version"]) is int
        and c["schema_version"] == 1,
        "invalid configuration schema",
    )
    if "platform" in c:
        platform_settings(c)
    for section, keys in FIELDS.items():
        workflow_common.require(
            isinstance(c[section], dict) and set(c[section]) == keys,
            "invalid fields: " + section,
        )
    for section, keys in FIELDS.items():
        for key in keys:
            if key.endswith(("_dir", "_root", "_bin", "_path")):
                value = c[section][key]
                workflow_common.require(
                    isinstance(value, str)
                    and value.startswith("/")
                    and "\x00" not in value
                    and ".." not in Path(value).parts
                    and len(Path(value).parts) > 2,
                    "fill an absolute path: " + section + "." + key,
                )
                if section != "local":
                    workflow_common.require(
                        re.fullmatch(r"/[A-Za-z0-9_./+\-]+", value),
                        "remote paths must use portable characters without spaces: "
                        + section
                        + "."
                        + key,
                    )
    workflow_common.require(
        isinstance(c["connection"]["environment"], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+", c["connection"]["environment"]),
        "invalid environment",
    )
    for section, key in [
        ("build", "jobs"),
        ("replay", "workers"),
        ("replay", "compile_threads"),
    ]:
        value = c[section][key]
        workflow_common.require(
            (key == "workers" and value == "auto") or type(value) is int and value > 0,
            "invalid positive integer: " + key,
        )
    t = c["replay"]["batch_timeout_seconds"]
    workflow_common.require(
        type(t) in (int, float) and math.isfinite(t) and t > 0, "invalid timeout"
    )
    for section, key in [("replay", "dump"), ("upload", "enabled")]:
        workflow_common.require(
            type(c[section][key]) is bool, "invalid boolean: " + key
        )
    devices = c["replay"]["devices"]
    workflow_common.require(
        devices == "auto"
        or isinstance(devices, list)
        and devices
        and all(isinstance(d, str) and re.fullmatch(r"npu:[0-9]+", d) for d in devices)
        and len(set(devices)) == len(devices),
        "invalid devices",
    )
    for key in FIELDS["sync"]:
        workflow_common.require(isinstance(c["sync"][key], list), "invalid sync paths")
        for value in c["sync"][key]:
            workflow_common.relative(value)
            workflow_common.require(
                not workflow_common.protected(value), "protected sync path: " + value
            )
    local = Path(c["local"]["source_dir"]).resolve()
    workflow_common.require(
        not workflow_common.inside(Path(c["local"]["state_dir"]).resolve(), local),
        "local state overlaps source",
    )
    source = Path(c["remote"]["source_dir"])
    for value in [
        c["remote"]["state_dir"],
        *[c["runtime"][k] for k in ("install_root", "venv_dir")],
        c["replay"]["results_dir"],
    ]:
        p = Path(value)
        workflow_common.require(
            not workflow_common.inside(p, source)
            and not workflow_common.inside(source, p),
            "remote paths overlap source: " + value,
        )
    for key in ("llvm_build_dir", "lld_bin"):
        workflow_common.require(
            not workflow_common.inside(Path(c["build"][key]), source),
            "toolchain lies within synchronized source",
        )
    return c


def platform_settings(c):
    p = c.get("platform", {})
    workflow_common.require(
        set(p) - {"client_token_file"} == {"url", "agent_id", "token_file", "remote_token_file"},
        "fill platform configuration",
    )
    workflow_common.require(isinstance(p["url"], str), "fill platform.url")
    url = urllib.parse.urlsplit(p["url"])
    workflow_common.require(
        url.scheme == "https"
        and url.netloc
        and not url.username
        and not url.password
        and not url.query
        and not url.fragment,
        "platform.url must be an HTTPS URL without credentials",
    )
    workflow_common.require(
        isinstance(p["agent_id"], str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", p["agent_id"]),
        "invalid platform.agent_id",
    )
    for key in ("token_file", "remote_token_file"):
        workflow_common.require(
            isinstance(p[key], str) and Path(p[key]).is_absolute(),
            "fill platform." + key,
        )
    if p.get("client_token_file") is not None:
        workflow_common.require(isinstance(p["client_token_file"], str) and Path(p["client_token_file"]).is_absolute(),
                                "platform.client_token_file must be an absolute remote path")
    return p


def default_config_path():
    """Resolve user configuration independently of the installed package."""
    import os

    if os.environ.get("AF_FORGE_CONFIG"):
        return Path(os.environ["AF_FORGE_CONFIG"]).expanduser()
    local = Path.cwd() / "af-forge-config.yaml"
    if local.is_file():
        return local
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "af-forge" / "config.yaml"


def write_template(source, output):
    """Create an annotated template without overwriting existing settings."""
    from importlib.resources import files
    import json

    source = Path(source).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source directory does not exist: {source}")
    text = files("af_forge").joinpath("resources/config.example.yaml").read_text(encoding="utf-8")
    text = text.replace("source_dir: null", "source_dir: " + json.dumps(str(source), ensure_ascii=False), 1)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(text)
    return {"config": str(output), "source_dir": str(source)}
