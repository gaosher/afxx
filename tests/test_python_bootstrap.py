#!/usr/bin/env python3
"""Managed Python preparation, capability failures and initialization ordering."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest
from afxx.environment import bootstrap as b
from afxx import common


@pytest.fixture
def managed(cfg, tmp_path):
    cfg["python_env"] = {
        "base_python": None,
        "uv_bin": None,
        "python_dir": str(tmp_path / "python"),
    }
    return cfg


def test_real_base_python_capabilities(tmp_path):
    report = b.check_python(sys.executable, tmp_path)
    assert report["passed"]
    assert {row["name"] for row in report["checks"]} >= {
        "ssl",
        "ctypes",
        "venv",
        "ensurepip",
        "sqlite>=3.35",
        "venv-create-and-pip",
    }
    assert common.read(tmp_path / "result.json")["base_python"]["passed"]


@pytest.mark.parametrize("module", ["ssl", "ctypes", "venv", "ensurepip", "sqlite3"])
def test_probe_reports_missing_module(module):
    prefix = "import sys\nsys.modules[" + repr(module) + "] = None\n"
    result = subprocess.run(
        [sys.executable, "-I", "-c", prefix + b.PROBE], capture_output=True, text=True
    )
    report = json.loads(result.stdout)
    assert not report["passed"]
    assert any(
        not row["passed"]
        and row["name"] == ("sqlite>=3.35" if module == "sqlite3" else module)
        for row in report["checks"]
    )
    assert not any(row["name"] == "venv-create-and-pip" for row in report["checks"])


def test_probe_reports_old_sqlite():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sqlite3; sqlite3.sqlite_version_info=(3,30,0)\n" + b.PROBE,
        ],
        capture_output=True,
        text=True,
    )
    assert not json.loads(result.stdout)["passed"]


def test_prepare_installs_once_and_honors_frozen_version(
    managed, tmp_path, monkeypatch
):
    directory = Path(managed["python_env"]["python_dir"])
    resolved = directory / "installations/cpython/bin/python3.11"
    resolved.parent.mkdir(parents=True)
    resolved.touch()
    managed["environment_lock"] = {"python": "Python 3.11.15"}
    calls = []
    monkeypatch.setattr(b, "bootstrap_uv", lambda _: "uv")
    monkeypatch.setattr(common, "run", lambda argv, **kw: calls.append((argv, kw)))
    monkeypatch.setattr(common, "capture", lambda argv, **kw: str(resolved))
    probes = []
    monkeypatch.setattr(
        b, "check_python", lambda path, job, expected: probes.append((path, expected))
    )
    first = b.prepare(managed, tmp_path / "job")
    assert Path(first).resolve() == resolved
    assert b.prepare(managed, tmp_path / "job") == first
    assert len(calls) == 1
    assert calls[0][0] == [
        "uv",
        "python",
        "install",
        "3.11.15",
        "--no-bin",
        "--no-config",
    ]
    assert calls[0][1]["env"]["UV_PYTHON_INSTALL_DIR"] == str(
        directory / "installations"
    )
    assert all(expected == "3.11.15" for _, expected in probes)


def test_failed_probe_does_not_publish_interpreter(managed, tmp_path, monkeypatch):
    directory = Path(managed["python_env"]["python_dir"])
    monkeypatch.setattr(b, "bootstrap_uv", lambda _: "uv")
    monkeypatch.setattr(common, "run", lambda *a, **k: None)
    monkeypatch.setattr(
        common, "capture", lambda *a, **k: str(directory / "installations/python")
    )

    def fail(*args):
        raise common.WorkflowError("PYTHON_BOOTSTRAP_FAILED", "sqlite too old")

    monkeypatch.setattr(b, "check_python", fail)
    with pytest.raises(common.WorkflowError, match="sqlite too old"):
        b.prepare(managed, tmp_path)
    assert not (directory / "bin/python3.11").exists()


@pytest.mark.parametrize("cached", [False, True])
def test_existing_environment_keeps_its_base(managed, tmp_path, monkeypatch, cached):
    if cached:
        managed_path = Path(managed["python_env"]["python_dir"]) / "bin/python3.11"
        managed_path.parent.mkdir(parents=True)
        managed_path.touch()
    Path(managed["runtime"]["venv_dir"]).mkdir(parents=True)
    monkeypatch.setattr(common, "capture", lambda *a, **k: sys.executable)
    monkeypatch.setattr(b, "check_python", lambda *a: None)
    monkeypatch.setattr(b, "bootstrap_uv", lambda *a: pytest.fail("must not download"))
    assert b.prepare(managed, tmp_path) == sys.executable


@pytest.mark.parametrize("bad_hash", [False, True])
def test_uv_download_hash_and_atomic_publish(tmp_path, monkeypatch, bad_hash):
    wheel = tmp_path / "fixture.whl"
    with zipfile.ZipFile(wheel, "w") as z:
        z.writestr("uv.data/scripts/uv", b"executable")
        z.writestr("../../unwanted", b"not extracted")
    metadata = {
        "urls": [
            {
                "filename": "uv-0.11.32-py3-none-manylinux_2_17_aarch64.whl",
                "url": "https://example.invalid/uv.whl",
                "digests": {"sha256": "0" * 64 if bad_hash else common.sha(wheel)},
            }
        ]
    }

    def download(url, dest):
        if str(dest).endswith("metadata.json"):
            common.write(dest, metadata)
        else:
            shutil.copyfile(wheel, dest)

    monkeypatch.setattr(b, "download", download)
    monkeypatch.setattr(b.platform, "system", lambda: "Linux")
    monkeypatch.setattr(b.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(common, "capture", lambda *a: "uv " + b.UV_VERSION)
    dest = tmp_path / "bootstrap"
    if bad_hash:
        with pytest.raises(ValueError, match="checksum"):
            b.bootstrap_uv(dest)
        assert not (dest / "uv").exists()
    else:
        assert Path(b.bootstrap_uv(dest)).read_bytes() == b"executable"
        assert not (tmp_path / "unwanted").exists()


def test_install_rejects_python_before_cann_or_target_venv(
    env_cfg, tmp_path, monkeypatch
):
    from afxx.environment import python as installer

    def fail(*a):
        raise common.WorkflowError("PYTHON_BOOTSTRAP_FAILED", "ssl unavailable")

    monkeypatch.setattr(b, "check_python", fail)
    monkeypatch.setattr(
        installer.af_cann_env, "ensure", lambda *a: pytest.fail("CANN must not run")
    )
    with pytest.raises(common.WorkflowError, match="ssl unavailable"):
        installer.install(env_cfg, tmp_path)
    assert not Path(env_cfg["runtime"]["venv_dir"]).exists()


from test_af_python_env import env_cfg as env_cfg
