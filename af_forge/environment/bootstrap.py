#!/usr/bin/env python3
"""Prepare a managed interpreter using only the remote worker's standard library."""

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import zipfile

from af_forge import common as common

UV_VERSION = "0.11.32"


def clean_env():
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PYTHON", "UV_", "PIP_")) and k != "VIRTUAL_ENV"
    }


PROBE = r"""
import importlib, json, sys, tempfile, subprocess
checks = []
def check(name, action):
    try:
        detail = action()
        checks.append(dict(name=name, passed=True, detail=str(detail)))
    except Exception as error:
        checks.append(dict(name=name, passed=False, detail=str(error)))
def version():
    assert sys.version_info[:2] == (3, 11), sys.version
    return sys.version.split()[0]
check("python==3.11", version)
for name in ("ssl", "ctypes", "venv", "ensurepip"):
    check(name, lambda name=name: importlib.import_module(name).__file__)
def sqlite():
    import sqlite3
    assert sqlite3.sqlite_version_info >= (3,35,0), sqlite3.sqlite_version
    with sqlite3.connect(":memory:") as db:
        db.execute("create table probe (value integer)")
        db.execute("insert into probe values (1) returning value").fetchone()
    return sqlite3.sqlite_version
check("sqlite>=3.35", sqlite)
def ssl_context():
    import ssl
    ssl.create_default_context()
    return ssl.OPENSSL_VERSION
check("ssl-context", ssl_context)
def venv():
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "venv"
        result = subprocess.run([sys.executable,"-I","-m","venv",str(target)], capture_output=True,text=True,timeout=120)
        assert result.returncode == 0, result.stderr
        result = subprocess.run([str(target/"bin/python"),"-I","-m","pip","--version"], capture_output=True,text=True,timeout=30)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
if all(row["passed"] for row in checks):
    check("venv-create-and-pip", venv)
print(json.dumps(dict(passed=all(row["passed"] for row in checks), version=sys.version.split()[0], checks=checks)))
"""


def check_python(path, job=None, expected=None):
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", PROBE],
            env=clean_env(),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode:
            raise ValueError(
                result.stderr.strip() or "interpreter exited " + str(result.returncode)
            )
        report = json.loads(result.stdout)
        if expected and report["version"] != expected:
            report["checks"].append(
                dict(
                    name="frozen-python",
                    passed=False,
                    detail=f"expected {expected}, actual {report['version']}",
                )
            )
            report["passed"] = False
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        report = {
            "passed": False,
            "checks": [{"name": "interpreter", "passed": False, "detail": str(error)}],
        }
    report["interpreter"] = str(path)
    if job is not None:
        common.update_record(Path(job) / "result.json", base_python=report)
    failures = [
        row["name"] + ": " + row["detail"]
        for row in report["checks"]
        if not row["passed"]
    ]
    common.require_code(
        report["passed"],
        "PYTHON_BOOTSTRAP_FAILED",
        "基础 Python 能力检查失败（尚未安装 CANN 或创建目标 venv）："
        + "; ".join(failures),
    )
    return report


def download(url, destination):
    common.require(url.startswith("https://"), "bootstrap download must use HTTPS")
    curl = shutil.which("curl")
    common.require_code(
        curl, "DEPENDENCY_MISSING", "远端缺少 curl，无法下载托管 Python 的引导工具"
    )
    common.run(
        [
            curl,
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "30",
            "--max-time",
            "600",
            "--retry",
            "2",
            "--output",
            destination,
            url,
        ]
    )


def bootstrap_uv(directory):
    binary = directory / "uv"
    if binary.is_file():
        common.require(
            common.capture([binary, "--version"]).split()[:2] == ["uv", UV_VERSION],
            "bootstrap uv version mismatch",
        )
        return str(binary)
    machine = platform.machine()
    common.require(
        platform.system() == "Linux" and machine in ("aarch64", "x86_64"),
        "managed bootstrap requires Linux aarch64/x86_64",
    )
    sources = common.read(Path(__file__).with_name("package_sources.json"))
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        tmp = Path(tmp)
        download(
            sources["uv_metadata"].format(version=UV_VERSION), tmp / "metadata.json"
        )
        metadata = common.read(tmp / "metadata.json")
        wheels = [
            item
            for item in metadata["urls"]
            if item["filename"].endswith(".whl")
            and ("manylinux_2_17_" + machine) in item["filename"]
            and not item.get("yanked")
        ]
        common.require(len(wheels) == 1, "no unique compatible uv bootstrap wheel")
        item = wheels[0]
        download(item["url"], tmp / "uv.whl")
        common.require(
            common.sha(tmp / "uv.whl") == item["digests"]["sha256"],
            "uv wheel checksum mismatch",
        )
        with zipfile.ZipFile(tmp / "uv.whl") as archive:
            names = [n for n in archive.namelist() if n.endswith("/uv")]
            common.require(len(names) == 1, "uv wheel must contain one executable")
            with archive.open(names[0]) as src, (tmp / "uv").open("wb") as dest:
                shutil.copyfileobj(src, dest)
        (tmp / "uv").chmod(0o700)
        common.require(
            common.capture([tmp / "uv", "--version"]).split()[:2] == ["uv", UV_VERSION],
            "downloaded uv version mismatch",
        )
        (tmp / "uv").replace(binary)
    return str(binary)


def prepare(c, job):
    settings = c["python_env"]
    expected = (
        c.get("environment_lock", {}).get("python", "").removeprefix("Python ") or None
    )
    if settings.get("base_python"):
        path = settings["base_python"]
        check_python(path, job, expected)
        return str(path)
    directory = Path(settings["python_dir"])
    common.require(
        directory.is_absolute()
        and ".." not in directory.parts
        and len(directory.parts) > 2,
        "invalid python_env.python_dir",
    )
    for other in (
        c["remote"]["source_dir"],
        c["runtime"]["venv_dir"],
        c["runtime"]["install_root"],
    ):
        common.require(
            not common.inside(directory.resolve(), Path(other).resolve())
            and not common.inside(Path(other).resolve(), directory.resolve()),
            "managed Python directory overlaps source, venv or CANN",
        )
    path = directory / "bin/python3.11"
    with common.lock(directory / ".bootstrap.lock"):
        # Never silently switch an existing environment to a newly downloaded interpreter.
        target = Path(c["runtime"]["venv_dir"])
        if target.exists():
            result = common.capture(
                [
                    target / "bin/python",
                    "-I",
                    "-c",
                    "import sys; print(sys._base_executable)",
                ],
                env=clean_env(),
            ).strip()
            check_python(result, job, expected)
            return result
        if path.exists():
            check_python(path, job, expected)
            return str(path)
        print("[python] 准备 workflow 专用 Python 3.11: " + str(directory), flush=True)
        uv = settings.get("uv_bin") or bootstrap_uv(directory / "bootstrap")
        env = clean_env()
        env.update(
            UV_PYTHON_INSTALL_DIR=str(directory / "installations"),
            UV_CACHE_DIR=str(directory / "cache"),
        )
        common.run(
            [uv, "python", "install", expected or "3.11", "--no-bin", "--no-config"],
            env=env,
        )
        resolved = common.capture(
            [
                uv,
                "python",
                "find",
                expected or "3.11",
                "--managed-python",
                "--no-python-downloads",
                "--no-project",
                "--no-config",
            ],
            env=env,
        ).strip()
        common.require(
            common.inside(
                Path(resolved).resolve(), (directory / "installations").resolve()
            ),
            "managed Python resolved outside workflow Python directory",
        )
        check_python(resolved, job, expected)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(Path(resolved).resolve())
        return str(path)
