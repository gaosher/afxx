#!/usr/bin/env python3
"""Create an isolated AF venv or update one managed torch_npu/AFX wheel on the NPU host."""

from af_forge import common as workflow_common
from af_forge import config as workflow_config
from af_forge import execution as workflow_execution
from af_forge.environment import runtime as workflow_runtime

import argparse
import datetime
import email
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import urllib.error

try:
    import tomllib
except ImportError:  # Host-side tests on Python <3.11. NPU runner uses Python 3.11.
    import tomli as tomllib
import uuid
import zipfile

from af_forge.environment import cann as af_cann_env
from af_forge.environment import bootstrap

FIELDS = {
    "uv_bin",
    "base_python",
    "python_dir",
    "state_dir",
    "index_url",
    "torch_index_url",
    "stable_requirements",
    "torch_npu",
    "afx",
}


MANAGED_UV = "uv==" + bootstrap.UV_VERSION


def require(condition, message):
    workflow_common.require(condition, message)


def url(value):
    p = urllib.parse.urlsplit(value)
    require(
        p.scheme == "https"
        and p.hostname
        and not p.username
        and not p.password
        and not p.query
        and not p.fragment,
        "artifact/index URL must be HTTPS without embedded credentials/query",
    )
    return value


def validate(c, mode, target):
    p = c.get("python_env")
    require(
        isinstance(p, dict) and set(p) == FIELDS,
        "fill the python_env configuration section",
    )
    # Resolve tools on the NPU host, after the configuration has been transferred.
    if p["base_python"] is None:
        p["base_python"] = sys.executable
    p.update(workflow_runtime.resolve_env_tools(p))
    if p["uv_bin"] is None:
        managed = Path(c["runtime"]["venv_dir"]) / "bin/uv"
        if mode == "update" and not managed.exists():
            # Environments initialized by older workflows used an external uv.
            p["uv_bin"] = shutil.which("uv")
            require(p["uv_bin"], "旧环境缺少 uv，请填写 python_env.uv_bin")
        else:
            p["uv_bin"] = str(managed)
    for key in ("uv_bin", "base_python", "state_dir"):
        require(
            isinstance(p[key], str)
            and Path(p[key]).is_absolute()
            and ".." not in Path(p[key]).parts,
            "invalid python_env." + key,
        )
    venv = Path(c["runtime"]["venv_dir"]).resolve()
    require(
        not workflow_common.inside(Path(p["state_dir"]).resolve(), venv),
        "environment state must be outside venv",
    )
    require(
        not workflow_common.inside(Path(p["base_python"]).resolve(), venv),
        "base Python must be outside target venv",
    )
    for key in ("index_url", "torch_index_url"):
        if p[key] is not None:
            url(p[key])
    requirements = p["stable_requirements"]
    require(
        isinstance(requirements, list) and requirements,
        "stable_requirements must be nonempty",
    )
    for value in requirements:
        require(
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9_.+<>=!,\[\] -]+", value)
            and not value.startswith("-"),
            "stable requirements must be package specifiers, not paths/URLs/options",
        )
        require(
            re.split(r"[<>=!\[ ]", value)[0].lower().replace("_", "-")
            not in {"torch-npu", "autofusex", "autofusedebugger"},
            "frequently updated components cannot be stable requirements",
        )
    require(
        any(re.fullmatch(r"torch==[A-Za-z0-9_.+]+", value) for value in requirements),
        "pin torch with torch==VERSION",
    )
    require(
        isinstance(p["torch_npu"], dict)
        and set(p["torch_npu"]) == {"version", "date", "url_template", "sha256"},
        "invalid torch_npu configuration",
    )
    require(
        isinstance(p["afx"], dict)
        and set(p["afx"]) in ({"wheel", "sha256"}, {"version", "wheel", "sha256"}),
        "invalid AFX configuration",
    )
    for name in ("torch_npu", "afx"):
        checksum = p[name]["sha256"]
        require(
            checksum is None
            or isinstance(checksum, str)
            and re.fullmatch("[0-9a-f]{64}", checksum),
            "invalid artifact sha256",
        )
    if target in (None, "torch_npu"):
        daily = p["torch_npu"]
        require(
            isinstance(daily["version"], str)
            and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", daily["version"]),
            "invalid torch_npu version",
        )
        require(
            isinstance(daily["date"], str)
            and (daily["date"] == "latest" or re.fullmatch("[0-9]{8}", daily["date"])),
            "torch_npu.date must be YYYYMMDD or latest",
        )
        if daily["date"] != "latest":
            datetime.datetime.strptime(daily["date"], "%Y%m%d")
        require(
            daily["date"] != "latest" or daily["sha256"] is None,
            "latest cannot reuse a checksum from an older Daily",
        )
        require(isinstance(daily["url_template"], str), "fill torch_npu.url_template")
        url(
            daily["url_template"].format(
                version=daily["version"], date=daily["date"], python_tag="py311"
            )
        )
    if target in (None, "afx"):
        wheel = p["afx"]["wheel"]
        version = p["afx"].get("version")
        require(
            (wheel is None) != (version is None),
            "set exactly one of afx.version or afx.wheel",
        )
        if version is not None:
            require(
                isinstance(version, str)
                and (
                    version == "latest"
                    or re.fullmatch(
                        r"[0-9]+(?:[.][0-9a-zA-Z]+)*(?:[a-zA-Z]+[0-9]+)?", version
                    )
                ),
                "afx.version must be latest or an exact release version",
            )
            return p
        require(
            isinstance(wheel, str) and wheel.endswith(".whl"),
            "fill afx.wheel with an absolute wheel path or HTTPS URL",
        )
        if wheel.startswith("https:"):
            url(wheel)
        else:
            require(
                Path(wheel).is_absolute() and Path(wheel).is_file(),
                "AFX wheel must exist on the NPU host",
            )
    return p


def clean_env():
    env = dict(os.environ)
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PIP_USER",
    ):
        env.pop(key, None)
    for key in list(env):
        if key.startswith(("UV_", "PIP_")):
            env.pop(key)
    env.update(
        PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1"
    )
    return env


def uv_env(c):
    env = clean_env()
    env["UV_CACHE_DIR"] = str(Path(c["python_env"]["state_dir"]) / "uv-cache")
    env["UV_PYTHON_DOWNLOADS"] = "never"
    return env


def uv(c, args, job):
    print(
        "uv " + " ".join(args[:2]) + " (log: " + str(job / "uv.log") + ")", flush=True
    )
    with (job / "uv.log").open("a") as log:
        return workflow_common.run(
            [c["python_env"]["uv_bin"], "--no-config", *args],
            env=uv_env(c),
            stdout=log,
            stderr=subprocess.STDOUT,
        )


def packages(c):
    text = workflow_common.capture(
        [
            c["python_env"]["uv_bin"],
            "--no-config",
            "pip",
            "list",
            "--python",
            workflow_runtime.python(c),
            "--format=json",
        ],
        env=uv_env(c),
    )
    return {
        re.sub("[-_.]+", "-", item["name"]).lower(): item["version"]
        for item in json.loads(text)
    }


def read_lock(path):
    path = Path(path)
    data = tomllib.loads(path.read_text())

    def normalize(value):
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, dict):
            return {
                k: str((path.parent / v).resolve()) if k == "path" else normalize(v)
                for k, v in value.items()
            }
        return value

    return {p["name"]: normalize(p) for p in data["packages"]}


def resolve(c, artifacts, job, previous=None, changed=()):
    p = c["python_env"]
    roots = list(
        dict.fromkeys(
            [
                "pip",
                "packaging",
                "nnsmith-autofusex==0.1.2.post1",
                *p["stable_requirements"],
            ]
        )
    )
    if Path(p["uv_bin"]) == Path(c["runtime"]["venv_dir"]) / "bin/uv":
        roots.append(MANAGED_UV)
    roots += [
        "torch-npu @ " + Path(artifacts["torch_npu"]["path"]).as_uri(),
        "autofusex[execution] @ " + Path(artifacts["afx"]["path"]).as_uri(),
    ]
    text = '[project]\nname = "af-forge-environment"\nversion = "0.0.0"\nrequires-python = ">=3.11,<3.12"\n'
    text += "dependencies = " + json.dumps(roots) + "\n"
    if p["index_url"]:
        text += (
            '\n[[tool.uv.index]]\nname = "default"\ndefault = true\nurl = '
            + json.dumps(p["index_url"])
            + "\n"
        )
    if p["torch_index_url"]:
        text += (
            '\n[[tool.uv.index]]\nname = "torch-cpu"\nexplicit = true\nurl = '
            + json.dumps(p["torch_index_url"])
            + "\n"
        )
        text += '\n[tool.uv.sources]\ntorch = { index = "torch-cpu" }\n'
    project = job / "pyproject.toml"
    project.write_text(text)
    locks = Path(p["state_dir"]) / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=locks) as staging:
        locked = Path(staging) / "pylock.toml"
        command = [
            "pip",
            "compile",
            str(project),
            "--python",
            workflow_runtime.python(c),
            "--no-header",
            "--format",
            "pylock.toml",
            "--output-file",
            str(locked),
        ]
        if previous:
            constraints = job / "stable-constraints.txt"
            constraints.write_text(
                "".join(
                    name + "==" + record["version"] + "\n"
                    for name, record in read_lock(previous).items()
                    if name not in changed and "version" in record
                )
            )
            command.extend(["--constraints", str(constraints)])
        uv(c, command, job)
        # uv writes absolute archive paths for direct wheel references. Keep the
        # lock portable across workflow roots without changing remote registry URLs.
        content = locked.read_text()
        for item in artifacts.values():
            wheel = Path(item["path"])
            relative = os.path.relpath(wheel, locked.parent)
            content = content.replace(
                "path = " + json.dumps(str(wheel)), "path = " + json.dumps(relative)
            )
        locked.write_text(content)
        if previous:
            old = {k: v for k, v in read_lock(previous).items() if k not in changed}
            new = {k: v for k, v in read_lock(locked).items() if k not in changed}
            require(
                new == old,
                "update would change stable dependencies or their sources; create a new environment instead",
            )
        # Both locations have the same depth so uv's relative wheel paths remain valid.
        destination = locks / workflow_common.sha(locked) / locked.name
        destination.parent.mkdir(exist_ok=True)
        locked.replace(destination)
    project.unlink()
    if previous:
        constraints.unlink()
    return destination


def sync(c, locked, job, component=None):
    args = ["pip", "sync", str(locked), "--python", workflow_runtime.python(c)]
    for name in component or ():
        args += ["--reinstall-package", name]
    uv(c, args, job)


def selected_daily(daily, tag, today=None):
    if daily["date"] != "latest":
        return daily["date"]
    today = today or datetime.datetime.now(datetime.timezone.utc).date()
    for offset in range(31):
        date = (today - datetime.timedelta(days=offset)).strftime("%Y%m%d")
        address = daily["url_template"].format(
            version=daily["version"], date=date, python_tag=tag
        )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(address, method="HEAD"), timeout=30
            ) as response:
                require(response.status == 200, "unexpected Daily response")
                return date
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise RuntimeError(
                    "Daily discovery failed with HTTP " + str(error.code)
                ) from None
    raise ValueError("no published Daily in the last 31 UTC dates")


def fetch(source, destination, checksum=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.startswith("https:"):
        url(source)
        try:
            with (
                urllib.request.urlopen(source, timeout=60) as response,
                destination.open("xb") as output,
            ):
                require(
                    urllib.parse.urlsplit(response.url).scheme == "https",
                    "insecure artifact redirect",
                )
                shutil.copyfileobj(response, output)
        except Exception as exc:
            raise RuntimeError(
                "artifact download failed: " + type(exc).__name__
            ) from None
    else:
        shutil.copyfile(source, destination)
    actual = workflow_common.sha(destination)
    require(checksum is None or checksum == actual, "artifact checksum mismatch")
    return actual


def wheel_info(path, expected):
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
        require(len(names) == 1, "wheel must contain exactly one METADATA file")
        info = email.message_from_bytes(archive.read(names[0]))
    require(
        bool(info["Name"]) and bool(info["Version"]),
        "wheel metadata lacks name/version",
    )
    name = re.sub("[-_.]+", "-", info["Name"]).lower()
    require(name == expected, "unexpected wheel distribution: " + name)
    return {
        "name": name,
        "version": info["Version"],
        "requires": info.get_all("Requires-Dist", []),
        "extras": info.get_all("Provides-Extra", []),
        "requires_python": info.get("Requires-Python"),
    }


def compatible_wheels(c, names):
    script = """import json,sys
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename
tags=set(sys_tags()); result=[]
for name in json.loads(sys.argv[1]):
 try:
  _,_,_,wheel_tags=parse_wheel_filename(name)
  if tags.intersection(wheel_tags): result.append(name)
 except ValueError: pass
print(json.dumps(result))
"""
    return json.loads(
        workflow_common.capture(
            [workflow_runtime.python(c), "-I", "-c", script, json.dumps(names)],
            env=clean_env(),
        )
    )


def artifact(c, target, job):
    cache = Path(c["python_env"]["state_dir"]) / "wheels"
    cache.mkdir(parents=True, exist_ok=True)
    pins = c.get("environment_lock")
    if pins:
        digest = pins["wheels"][target]
        require(
            isinstance(digest, str) and re.fullmatch("[0-9a-f]{64}", digest),
            "invalid frozen wheel hash",
        )
        for path in sorted((cache / digest).glob("*.whl")):
            require(
                workflow_common.sha(path) == digest,
                "cached wheel checksum mismatch: " + path.name,
            )
            if compatible_wheels(c, [path.name]):
                dist = "autofusex" if target == "afx" else "torch-npu"
                metadata = wheel_info(path, dist)
                if target == "afx" and c["python_env"]["afx"].get("version") not in (
                    None,
                    "latest",
                ):
                    require(
                        metadata["version"] == c["python_env"]["afx"]["version"],
                        "cached AFX wheel version mismatch",
                    )
                return {
                    "path": str(path),
                    "sha256": digest,
                    "metadata": metadata,
                    "source": dict(c["python_env"][target]),
                }
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        return cache_wheel(c, target, job, Path(staging), cache)


def cache_wheel(c, target, job, staging, cache):
    p = c["python_env"]
    if target == "afx":
        source = p["afx"]["wheel"]
        if source is None:
            version = p["afx"]["version"]
            # Ignore host pip configuration so the default official PyPI index is used.
            env = dict(clean_env(), PIP_CONFIG_FILE=os.devnull)
            workflow_common.run(
                [
                    workflow_runtime.python(c),
                    "-I",
                    "-m",
                    "pip",
                    "download",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--dest",
                    staging,
                    "autofusex" if version == "latest" else "autofusex==" + version,
                ],
                env=env,
                stdout=sys.stderr,
            )
            candidates = list(staging.glob("*.whl"))
            require(len(candidates) == 1, "expected one autofusex wheel from PyPI")
            path = candidates[0]
            name = path.name
            require(
                not p["afx"]["sha256"]
                or workflow_common.sha(path) == p["afx"]["sha256"],
                "AFX wheel checksum mismatch",
            )
        else:
            name = Path(urllib.parse.urlsplit(source).path).name
            path = staging / name
            fetch(source, path, p["afx"]["sha256"])
        require(
            compatible_wheels(c, [name]) == [name],
            "AFX wheel incompatible with this interpreter/platform",
        )
        info = wheel_info(path, "autofusex")
        if source is None and version != "latest":
            require(
                info["version"] == version,
                "AFX wheel version differs from configured version",
            )
        require(
            "execution" in info["extras"],
            "AFX wheel must provide execution dependencies",
        )
    else:
        tag = workflow_common.capture(
            [
                workflow_runtime.python(c),
                "-I",
                "-c",
                'import sys; print(f"py{sys.version_info.major}{sys.version_info.minor}")',
            ],
            env=clean_env(),
        ).strip()
        daily = p["torch_npu"]
        date = selected_daily(daily, tag)
        source = daily["url_template"].format(
            version=daily["version"], date=date, python_tag=tag
        )
        workflow_common.update_record(
            job / "result.json",
            daily_selection={"date": date, "url": source, "python_tag": tag},
        )
        archive_path = staging / "torch_npu_daily.tar.gz"
        archive_sha = fetch(source, archive_path, daily["sha256"])
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [
                m
                for m in archive.getmembers()
                if m.isfile()
                and Path(m.name).name.startswith("torch_npu-")
                and m.name.endswith(".whl")
            ]
            matches = compatible_wheels(c, [Path(m.name).name for m in members])
            candidates = [m for m in members if Path(m.name).name in matches]
            require(
                len(candidates) == 1,
                "Daily archive must have exactly one compatible torch_npu wheel",
            )
            member = candidates[0]
            path = staging / Path(member.name).name
            # Copy only a regular wheel to a controlled basename; never extract archive paths.
            with archive.extractfile(member) as incoming, path.open("xb") as output:
                shutil.copyfileobj(incoming, output)
        info = wheel_info(path, "torch-npu")
        require(
            info["version"].split(".post")[0].split(".dev")[0] == daily["version"],
            "Daily wheel version differs from configured series",
        )
    digest = workflow_common.sha(path)
    dest = cache / digest / path.name
    dest.parent.mkdir(exist_ok=True)
    if not dest.exists() or workflow_common.sha(dest) != digest:
        path.replace(dest)
    source_config = dict(p[target])
    if target == "torch_npu":
        source_config.update(date=date, sha256=archive_sha)
    elif source_config.get("version") == "latest":
        source_config["version"] = info["version"]
    return {
        "path": str(dest),
        "sha256": digest,
        "metadata": info,
        "source": source_config,
    }


def verify(c, job, rollback=False, *, initial=False):
    uv(c, ["pip", "check", "--python", workflow_runtime.python(c)], job)
    workflow_runtime.venv_preflight(c, workflow_runtime.environment(c), job, "deploy")
    if initial or not workflow_runtime.registry(c).with_suffix(".json").exists():
        # deploy verifies AF libraries after installing its freshly built package.
        return
    evidence = workflow_runtime.probe(c, workflow_runtime.environment(c), job)
    if rollback:
        previous = workflow_common.read(job / "result.json").get("rollback", {})
        workflow_common.update_record(
            job / "result.json", rollback=dict(previous, runtime=evidence)
        )
    else:
        workflow_common.update_record(job / "result.json", runtime=evidence)


def state_path(c):
    return workflow_runtime.env_manifest_path(c)


def ready(c, artifacts, locked, cann, cann_config):
    return {
        "state": "ready",
        "manager": "uv",
        "packages": packages(c),
        "runtime": c["runtime"],
        "connection": c["connection"],
        "stable_requirements": c["python_env"]["stable_requirements"],
        "cann": cann,
        "cann_config": cann_config,
        "artifacts": artifacts,
        "lock": {"path": str(locked), "sha256": workflow_common.sha(locked)},
        "python": workflow_common.capture(
            [workflow_runtime.python(c), "--version"], env=clean_env()
        ).strip(),
        "uv_version": workflow_common.capture(
            [c["python_env"]["uv_bin"], "--version"], env=uv_env(c)
        ).strip(),
    }


def restore_lock(c, pins):
    import hashlib

    content = pins["pylock"].encode()
    digest = hashlib.sha256(content).hexdigest()
    require(digest == pins["pylock_sha256"], "frozen Python lock hash mismatch")
    path = Path(c["python_env"]["state_dir"]) / "locks" / digest / "pylock.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        out.write(content)
    Path(out.name).replace(path)
    return path


def installed_cann(c, job):
    report = workflow_common.read(job / "cann-bootstrap.json")
    receipt = Path(c["runtime"]["install_root"]) / ".af-cann-bootstrap.json"
    source = workflow_common.read(receipt) if receipt.is_file() else report
    release = report.get("installed_release") or source.get("release")
    require(
        isinstance(release, str) and re.fullmatch(r"\d{8,}", release),
        "missing CANN release timestamp",
    )
    return {
        "version": report.get("installed_version") or source["version"].split("~")[0],
        "release": release,
        "date": release[:8],
        "packages": source.get("packages", []),
        "artifacts": [
            {"name": Path(item["path"]).name, "sha256": item["sha256"]}
            for item in source.get("artifacts", [])
        ],
        "hardware": source.get("hardware", []),
    }


def install(c, job):
    p = c["python_env"]
    target = Path(c["runtime"]["venv_dir"])
    require(
        not target.exists() and not target.is_symlink(),
        "install requires a new, nonexistent venv directory",
    )
    if state_path(c).is_file():
        previous = workflow_common.read(state_path(c))
        require(
            c.get("environment_lock") and previous.get("runtime") == c["runtime"],
            "environment state directory already contains a manifest; use a fresh workflow_root",
        )
    bootstrap.check_python(p["base_python"], job)
    af_cann_env.ensure(c, job)
    managed_uv = Path(p["uv_bin"]) == target / "bin/uv"
    if managed_uv:
        workflow_common.run(
            [p["base_python"], "-I", "-m", "venv", str(target)], env=clean_env()
        )
    else:
        uv(c, ["venv", "--python", p["base_python"], "--seed", str(target)], job)
    workflow_common.write(state_path(c), {"state": "installing", "manager": "uv"})
    try:
        if managed_uv:
            index = ["--index-url", p["index_url"]] if p["index_url"] else []
            workflow_common.run(
                [
                    workflow_runtime.python(c),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    *index,
                    MANAGED_UV,
                ],
                env=clean_env(),
            )
        # Bootstrap only the wheel-tag reader; the final complete lock includes it too.
        uv(
            c,
            ["pip", "install", "--python", workflow_runtime.python(c), "packaging"],
            job,
        )
        artifacts = {name: artifact(c, name, job) for name in ("torch_npu", "afx")}
        pins = c.get("environment_lock")
        if pins:
            require(
                workflow_common.capture(
                    [workflow_runtime.python(c), "--version"], env=clean_env()
                ).strip()
                == pins["python"],
                "frozen Python interpreter version mismatch",
            )
            require(
                {k: v["sha256"] for k, v in artifacts.items()} == pins["wheels"],
                "frozen wheel hash mismatch",
            )
            locked = restore_lock(c, pins)
        else:
            locked = resolve(c, artifacts, job)
        sync(c, locked, job)
        verify(c, job, initial=True)
        cann = installed_cann(c, job)
        manifest = ready(
            c, artifacts, locked, cann, dict(c.get("cann", {}), date=cann["date"])
        )
        if pins:
            require(
                manifest["packages"] == pins["packages"],
                "frozen Python package inventory mismatch",
            )
            require(
                cann == pins["cann"], "frozen CANN release or package hashes differ"
            )
        workflow_common.write(state_path(c), manifest)
    except BaseException:
        workflow_common.write(state_path(c), {"state": "invalid", "manager": "uv"})
        raise


def planned_components(c, before, job):
    p = c["python_env"]
    plan = []
    for component in ("torch_npu", "afx"):
        previous = before["artifacts"][component]
        installed = previous["source"]
        desired = p[component]
        if component == "torch_npu":
            selected = selected_daily(desired, "py311")
            if (
                selected == installed["date"]
                and desired["sha256"] in (None, installed["sha256"])
                and all(desired[k] == installed[k] for k in ("version", "url_template"))
            ):
                continue
            # Resolve latest only once, including across UTC midnight.
            selected_config = dict(
                c, python_env=dict(p, torch_npu=dict(desired, date=selected))
            )
            plan.append((component, artifact(selected_config, component, job)))
            continue
        elif desired.get("version") == "latest":
            candidate = artifact(c, component, job)
            if candidate["sha256"] != previous["sha256"]:
                plan.append((component, candidate))
            continue
        elif desired.get("version"):
            if desired["version"] == installed.get("version") and desired["sha256"] in (
                None,
                previous["sha256"],
            ):
                continue
        else:
            source = desired["wheel"]
            checksum = desired["sha256"]
            if Path(source).is_absolute():
                actual = workflow_common.sha(source)
                require(
                    not checksum or checksum == actual,
                    "AFX configured wheel checksum mismatch",
                )
                checksum = actual
            if source == installed["wheel"] and checksum == previous["sha256"]:
                continue
            if source == installed["wheel"] and checksum is None:
                candidate = artifact(c, component, job)
                if candidate["sha256"] != previous["sha256"]:
                    plan.append((component, candidate))
                continue
        plan.append((component, artifact(c, component, job)))
    return plan


def update(c, job, target):
    marker = state_path(c)
    require(
        marker.is_file(), "update only accepts environments created by this installer"
    )
    before = workflow_common.read(marker)
    require(
        before["state"] == "ready" and before.get("manager") == "uv",
        "requires a ready uv-managed environment",
    )
    require(
        c["runtime"] == before["runtime"] and c["connection"] == before["connection"],
        "environment paths or connection differ from manifest",
    )
    require(
        c["python_env"]["stable_requirements"] == before["stable_requirements"],
        "stable requirements changed; initialize a new environment",
    )
    require(
        packages(c) == before["packages"],
        "environment changed outside installer; recreate or reconcile before update",
    )
    previous_lock = Path(before["lock"]["path"])
    require(
        workflow_common.sha(previous_lock) == before["lock"]["sha256"],
        "previous lock missing or changed",
    )
    for item in before["artifacts"].values():
        require(
            workflow_common.sha(item["path"]) == item["sha256"],
            "cached wheel missing or changed",
        )
    artifacts = dict(before["artifacts"])
    plan = (
        [(target, artifact(c, target, job))]
        if target
        else planned_components(c, before, job)
    )
    workflow_common.update_record(
        job / "result.json", components=[name for name, _ in plan]
    )
    if not plan:
        print("torch_npu and AFX already match configuration", flush=True)
        return
    artifacts.update(plan)
    changed = ["torch-npu" if name == "torch_npu" else "autofusex" for name, _ in plan]
    locked = resolve(c, artifacts, job, previous=previous_lock, changed=changed)
    workflow_common.write(marker, dict(before, state="updating"))
    try:
        sync(c, locked, job, changed)
        actual = packages(c)
        require(
            {k: v for k, v in actual.items() if k not in changed}
            == {k: v for k, v in before["packages"].items() if k not in changed},
            "update changed other packages",
        )
        verify(c, job)
        workflow_common.write(
            marker, ready(c, artifacts, locked, before["cann"], before["cann_config"])
        )
    except BaseException as failure:
        workflow_common.update_record(
            job / "result.json",
            state="failed",
            error=str(failure),
            rollback={"state": "running"},
        )
        workflow_common.write(marker, dict(before, state="invalid"))
        try:
            sync(c, previous_lock, job, changed)
            verify(c, job, True)
            require(
                packages(c) == before["packages"], "rollback package inventory differs"
            )
            workflow_common.write(marker, before)
            workflow_common.update_record(
                job / "result.json",
                rollback=dict(
                    workflow_common.read(job / "result.json")["rollback"],
                    state="restored",
                ),
            )
        except BaseException as error:
            workflow_common.update_record(
                job / "result.json",
                rollback=dict(
                    workflow_common.read(job / "result.json")["rollback"],
                    state="failed",
                    error=str(error),
                ),
            )
        raise


def main():
    if len(sys.argv) == 3 and sys.argv[1] in ("_init_job", "_update_job"):
        job = Path(sys.argv[2]).resolve()
        c = workflow_config.validate_config(workflow_common.read(job / "config.yaml"))
        mode = {"_init_job": "init", "_update_job": "update"}[sys.argv[1]]
        validate(c, mode, None)
        return execute(c, job, mode, None)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["init", "update"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--component", choices=["torch_npu", "afx"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    require(
        args.mode == "update" or not args.component, "init does not accept --component"
    )
    c = workflow_config.config(args.config)
    validate(c, args.mode, args.component)
    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": args.mode,
                    "component": args.component,
                    "venv": c["runtime"]["venv_dir"],
                    "dry_run": True,
                }
            )
        )
        return 0
    require(sys.platform == "linux", "run this installer on the Linux NPU host")
    os.umask(0o077)

    def interrupted(signum, frame):
        raise RuntimeError("interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    job = Path(c["remote"]["state_dir"]) / uuid.uuid4().hex
    job.mkdir(parents=True, mode=0o700)
    workflow_common.write(job / "config.yaml", c)
    return execute(c, job, args.mode, args.component)


def execute(c, job, mode, component):
    workflow_common.update_record(
        job / "result.json", state="running", mode=mode, component=component
    )
    print("Environment task: " + str(job), flush=True)
    # Same CANN lock as deployment/replay, plus a canonical venv lock independent of config.
    env_lock = workflow_runtime.venv_lock(c)
    with (
        workflow_common.lock(state_path(c).with_suffix(".lock")),
        workflow_common.lock(env_lock),
        workflow_common.lock(workflow_runtime.registry(c).with_suffix(".lock")),
    ):
        try:
            if Path(c["runtime"]["venv_dir"]).exists():
                workflow_runtime.check_idle(c["runtime"]["venv_dir"])
            if mode == "init":
                install(c, job)
            else:
                update(c, job, component)
            workflow_execution.save_frozen_config(
                c, job, workflow_common.read(state_path(c))
            )
            workflow_common.update_record(job / "result.json", state="success")
        except BaseException as error:
            workflow_common.update_record(
                job / "result.json", state="failed", error=str(error)
            )
            raise
    print("Environment ready: " + c["runtime"]["venv_dir"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        sys.exit(2)
