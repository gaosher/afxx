#!/usr/bin/env python3
"""Runtime environment probes and deployment identity checks."""

import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from af_forge import common as workflow_common


def registry(c):
    directory = Path(tempfile.gettempdir()) / ("af-workflow-" + str(os.getuid()))
    directory.mkdir(mode=0o700, exist_ok=True)
    workflow_common.require(
        directory.stat().st_uid == os.getuid() and not directory.is_symlink(),
        "unsafe lock directory",
    )
    return directory / workflow_common.digest(
        str(Path(c["runtime"]["install_root"]).resolve())
    )


def venv_lock(c):
    return registry(c).parent / (
        "venv-"
        + workflow_common.digest(str(Path(c["runtime"]["venv_dir"]).resolve()))
        + ".lock"
    )


def environment(c):
    root = Path(c["runtime"]["install_root"]) / "cann"
    venv = Path(c["runtime"]["venv_dir"])
    # Source vendor scripts without nounset; use the selected interpreter to serialize env.
    script = 'source "$1" && source "$2" && "$3" -c "import os,json; print(json.dumps(dict(os.environ)))"'
    base_env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}
    }
    text = workflow_common.capture(
        [
            "bash",
            "-c",
            script,
            "workflow",
            root / "set_env.sh",
            venv / "bin/activate",
            venv / "bin/python",
        ],
        env=base_env,
    )
    env = json.loads(text.splitlines()[-1])
    env.update(
        ASCEND_HOME_PATH=str(root),
        ASCEND_INSTALL_PATH=str(root),
        LLVM_BUILD_DIR=c["build"]["llvm_build_dir"],
        LLD_BIN=c["build"]["lld_bin"],
    )
    env["PYTHONPATH"] = (
        str(root / "python/site-packages") + os.pathsep + env.get("PYTHONPATH", "")
    )
    return env


def python(c):
    return str(Path(c["runtime"]["venv_dir"]) / "bin/python")


def dependency_checks(module, scope):
    """Explicit imports supplement installed distribution metadata (pip check)."""
    modules = ["numpy", "sympy", "torch", "torch_npu", module]
    if scope != "deploy":
        modules += ["nnsmith", "z3"]
    if scope in ("deploy", "full"):
        modules = ["pip", "build", "wheel", "setuptools", "packaging"] + modules
    checks = []
    for name in modules:
        script = (
            "import importlib,json; m=importlib.import_module(" + repr(name) + "); "
            'print(json.dumps({"module":m.__name__,"file":getattr(m,"__file__",None),'
            '"version":str(getattr(m,"__version__","unknown"))}))'
        )
        checks.append((name, [sys.executable, "-c", script]))
    if scope != "deploy":
        client = 'import importlib\nprefix="autofusex.fuzz.client"\n'
        checks.append(
            (
                "fuzz-cli",
                [
                    sys.executable,
                    "-c",
                    client + "print(importlib.import_module(prefix+'.cli').__file__)",
                ],
            )
        )
        checks.append(
            (
                "afx-contracts",
                [
                    sys.executable,
                    "-c",
                    "from autofusex.fuzz.client.generation.sampling import SamplingConfig,decode; "
                    "from autofusex.fuzz.client.storage import ResultRepository; "
                    "s=SamplingConfig(dynamic_variants=8).seed(0,dynamic=True); "
                    "assert decode(s)[0].dynamic_variants==8; "
                    "assert all(callable(f) for f in (ResultRepository.list,ResultRepository.detail)); "
                    'print("AFX seed/results interfaces available")',
                ],
            )
        )
    if scope in ("deploy", "full"):
        checks.append(
            (
                "setuptools>=68",
                [
                    sys.executable,
                    "-c",
                    "from importlib.metadata import version; from packaging.version import Version; "
                    'v=version("setuptools"); assert Version(v)>=Version("68"), v; print(v)',
                ],
            )
        )
    if scope != "deploy":
        checks.append(
            (
                "sqlite>=3.35",
                [
                    sys.executable,
                    "-c",
                    "import sqlite3; assert sqlite3.sqlite_version_info >= (3,35,0), sqlite3.sqlite_version; print(sqlite3.sqlite_version)",
                ],
            )
        )
        gate = """import importlib,importlib.util,json
prefix+='.generation.'
name=prefix+'runtime'
m=importlib.import_module(name)
print(json.dumps(m.source_evidence()))
"""
        checks.append(("nnsmith-source-and-z3", [sys.executable, "-c", client + gate]))
    if scope == "full":
        checks.append(("pip-check", [sys.executable, "-I", "-m", "pip", "check"]))
    return checks


def python_dependency_report(module, scope, expected_venv):
    report = {
        "interpreter": sys.executable,
        "prefix": sys.prefix,
        "scope": scope,
        "checks": [],
    }
    valid = (
        Path(sys.prefix).resolve() == Path(expected_venv).resolve()
        and sys.prefix != sys.base_prefix
    )
    report["checks"].append(
        {
            "name": "venv-interpreter",
            "passed": valid,
            "detail": "expected prefix: " + expected_venv,
        }
    )
    for name, command in dependency_checks(module, scope):
        try:
            result = workflow_common.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )
            report["checks"].append(
                {
                    "name": name,
                    "passed": result.returncode == 0,
                    "exit_code": result.returncode,
                    "stdout": result.stdout[-12000:],
                    "stderr": result.stderr[-12000:],
                }
            )
        except subprocess.TimeoutExpired:
            report["checks"].append(
                {
                    "name": name,
                    "passed": False,
                    "detail": "import/check timed out after 60s",
                }
            )
    report["passed"] = all(item["passed"] for item in report["checks"])
    return report


def venv_preflight(c, env, job, scope):
    workflow_common.phase(job, "python_dependencies")
    result = workflow_common.run(
        [
            python(c),
            "-m",
            "af_forge.environment.runtime",
            "_python_check",
            "autofusex",
            scope,
            c["runtime"]["venv_dir"],
        ],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        report = json.loads(result.stdout)
    except (ValueError, TypeError):
        report = {
            "passed": False,
            "checks": [],
            "error": "dependency checker could not run",
            "stderr": result.stderr[-12000:],
            "exit_code": result.returncode,
        }
    workflow_common.write(job / "python-dependencies.json", report)
    failed = [item["name"] for item in report["checks"] if not item["passed"]]
    workflow_common.require(
        result.returncode == 0 and report["passed"],
        "Python dependency preflight failed: "
        + ", ".join(failed)
        + "; see python-dependencies.json",
    )
    return report


def probe(c, env, job):
    code = (
        'import json,sys,importlib\nprefix="autofusex.fuzz.client"\n'
        + """
m=importlib.import_module(prefix+'.backends.mlir_capability')
p=importlib.import_module('autofuse.pyautofuse')
print(json.dumps({'pyautofuse':p.__file__,'adapter':m.verify_compile_adapter(),'codegen':m.verify_runtime(),
'debugger':importlib.import_module(sys.argv[1]).__file__}))
"""
    )
    paths = json.loads(
        workflow_common.capture(
            [python(c), "-c", code, "autofusex"], env=env, cwd=job
        ).splitlines()[-1]
    )
    root = Path(c["runtime"]["install_root"]).resolve()
    for key in ("pyautofuse", "adapter", "codegen"):
        workflow_common.require(
            workflow_common.inside(Path(paths[key]).resolve(), root),
            "loaded outside installation: " + key,
        )
    evidence = {
        k: {"path": str(Path(v).resolve()), "sha256": workflow_common.sha(v)}
        for k, v in paths.items()
    }
    package = Path(paths["debugger"]).resolve().parent
    evidence["debugger"]["tree_sha256"] = workflow_common.digest(
        {
            str(p.relative_to(package)): workflow_common.sha(p)
            for p in package.rglob("*.py")
        }
    )
    return evidence


def packaged_libraries(build):
    """Use the package component's install manifest, not unrelated CANN libraries."""
    manifest = Path(build) / "install_manifest_graph-autofusion.txt"
    names = {Path(line).name for line in manifest.read_text().splitlines()
             if re.search(r"\.so(?:\.|$)", Path(line).name)}
    workflow_common.require(names, "AF package library manifest is empty")
    return names


def check_idle(install_root, *, library_names=None):
    """Reject visible processes using the installation before overwriting its libraries."""
    root = str(Path(install_root).resolve()) + "/"
    busy = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            maps = (process / "maps").read_text()
        except FileNotFoundError:
            continue
        except PermissionError:
            # Some unrelated processes deny ptrace/maps even to the same UID.
            # Reject visible users of this prefix, without treating every such PID as busy.
            try:
                command = (process / "cmdline").read_bytes().decode(errors="replace")
                executable = os.readlink(process / "exe")
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
            if library_names is None and (root in command or executable.startswith(root)):
                busy.append(process.name)
            continue
        for line in maps.splitlines():
            fields = line.split(None, 5)
            if len(fields) < 6:
                continue
            path = fields[5].removesuffix(" (deleted)")
            if path.startswith(root) and (library_names is None or Path(path).name in library_names):
                busy.append(process.name)
                break
    workflow_common.require(
        not busy, "installation in use by processes: " + ", ".join(busy)
    )


def deployment_reference(path):
    path = Path(path).resolve()
    deployment = workflow_common.read(path)
    return {
        "state": deployment["state"],
        "job": deployment["job"],
        "deployment": {"path": str(path), "sha256": workflow_common.sha(path)},
    }


def current_deployment(c, path):
    path = Path(path).resolve()
    deployment = workflow_common.read(path)
    current = workflow_common.read(registry(c).with_suffix(".json"))
    if "deployment" in current:
        workflow_common.require(
            current == deployment_reference(path),
            "deployment is no longer current or changed",
        )
        source = deployment["source"]
        manifest_path = Path(source["path"])
        workflow_common.require(
            manifest_path.is_file()
            and workflow_common.sha(manifest_path) == source["sha256"],
            "source manifest missing or changed",
        )
        manifest = workflow_common.read(manifest_path)
        workflow_common.require(
            manifest["sha256"]
            == source["snapshot_sha256"]
            == workflow_common.digest(manifest["files"])
            and manifest["base_commit"] == source["base_commit"],
            "source manifest identity mismatch",
        )
    else:
        # Existing installed deployments keep their historical self-contained records.
        workflow_common.require(
            current == deployment, "deployment is no longer current"
        )
    workflow_common.require(
        deployment["state"] == "success"
        and deployment["environment"] == c["connection"]["environment"]
        and deployment["install_root"]
        == str(Path(c["runtime"]["install_root"]).resolve()),
        "deployment/config mismatch",
    )
    return deployment


def env_manifest_path(c):
    return Path(c["python_env"]["state_dir"]) / "current.json"


def resolve_env_tools(settings):
    paths = {}
    for key, name in (("base_python", "python3.11"), ("uv_bin", "uv")):
        configured = settings.get(key)
        if key == "uv_bin" and configured is None:
            continue
        if configured is not None:
            workflow_common.require(
                isinstance(configured, str)
                and Path(configured).is_absolute()
                and ".." not in Path(configured).parts,
                "invalid python_env." + key,
            )
        if key == "base_python" and configured is None:
            managed = Path(settings["python_dir"]) / "bin/python3.11"
            configured = str(managed)
        path = shutil.which(configured or name)
        workflow_common.require(
            path,
            f"远端找不到可执行的 {name}，请在 python_env.{key} 填写其远端绝对路径。",
        )
        paths[key] = path
    return paths


def check_main(args):
    def interrupted(signum, frame):
        raise RuntimeError("dependency check interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    report = python_dependency_report(*args)
    print(json.dumps(report))
    return 0 if report["passed"] else 1


def af_identity(env):
    version, sha256 = env.get("cann"), env.get("autofuse_binary")
    workflow_common.require(
        isinstance(version, str)
        and version
        and isinstance(sha256, str)
        and re.fullmatch("[0-9a-f]{64}", sha256),
        "AFX cannot identify installed AF",
    )
    return {"version": version, "sha256": sha256}


if __name__ == "__main__":
    sys.exit(check_main(sys.argv[2:]))
