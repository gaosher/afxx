import json
from pathlib import Path

import pytest

from af_forge import common, execution
from af_forge.environment import runtime, snapshot


def test_live_snapshot_preserves_config_without_state_or_wheel(
    cfg, tmp_path, monkeypatch
):
    job = tmp_path / "job"
    common.write(job / "config.yaml", cfg)
    before = (job / "config.yaml").read_bytes()
    live = {
        "version": "3.11.9",
        "packages": {
            "autofusex": {
                "version": "0.9.0",
                "source": {
                    "url": "file:///removed/afx.whl",
                    "archive_info": {"hashes": {"sha256": "a" * 64}},
                },
            }
        },
    }
    monkeypatch.setattr(runtime, "environment", lambda c: {})
    monkeypatch.setattr(common, "capture", lambda *a, **kw: json.dumps(live))
    monkeypatch.setattr(
        runtime,
        "probe",
        lambda *a: {"codegen": {"path": "/live/lib.so", "sha256": "b" * 64}},
    )
    execution.record_environment(cfg, job)
    data = common.read(job / "environment.json")
    assert data["python"] == live
    assert data["af"]["codegen"]["sha256"] == "b" * 64
    assert (job / "config.yaml").read_bytes() == before
    # Missing CANN metadata is recorded, not inferred from a stale init receipt.
    assert data["errors"]


def test_unavailable_af_is_recorded_for_first_deploy(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "environment", lambda c: {})
    monkeypatch.setattr(
        common, "capture", lambda *a, **kw: '{"version":"3.11.9","packages":{}}'
    )

    def unavailable(*args):
        raise ValueError("AF not installed")

    monkeypatch.setattr(runtime, "probe", unavailable)
    report = snapshot.capture(cfg, tmp_path)
    assert {"component": "af", "error": "AF not installed"} in report["errors"]
    assert "af" not in report


def test_package_origin_does_not_expose_url_credentials(monkeypatch, tmp_path):
    import subprocess
    import sys

    script = snapshot.PACKAGE_PROBE.replace(
        "for dist in md.distributions():",
        """
class Dist:
    metadata = {'Name': 'autofusex'}
    version = '0.9.0'
    def locate_file(self, path): return '/venv/site-packages'
    def read_text(self, path): return json.dumps({'url':'https://user:secret@host/a.whl?token=secret#secret','archive_info':{'hashes':{'sha256':'abc'}}})
for dist in [Dist()]:""",
    )
    output = subprocess.check_output([sys.executable, "-c", script], text=True)
    assert "secret" not in output
    source = json.loads(output)["packages"]["autofusex"]["source"]
    assert source["url"] == "https://host/a.whl"
    assert source["archive_info"]["hashes"]["sha256"] == "abc"


def test_deploy_occupancy_only_packaged_libraries(tmp_path, monkeypatch):
    root = tmp_path / "cann"
    root.mkdir()
    proc = tmp_path / "proc"
    pid = proc / "123456789"
    pid.mkdir(parents=True)
    maps = pid / "maps"
    maps.write_text(f"1-2 r-xp 0 00:00 1 {root}/lib64/libascendcl.so\n")
    original = Path.iterdir
    monkeypatch.setattr(
        Path, "iterdir", lambda p: original(proc if p == Path("/proc") else p)
    )
    runtime.check_idle(root, library_names={"pyautofuse.so"})
    # Whole-venv checks keep their previous behavior.
    with pytest.raises(ValueError, match="installation in use"):
        runtime.check_idle(root)
    maps.write_text(f"1-2 r-xp 0 00:00 1 {root}/python/pyautofuse.so (deleted)\n")
    with pytest.raises(ValueError, match="123456789"):
        runtime.check_idle(root, library_names={"pyautofuse.so"})


def test_package_manifest_limits_occupancy_scope(tmp_path):
    (tmp_path / "install_manifest_graph-autofusion.txt").write_text(
        "/stage/lib64/libascir.so\n/stage/python/autofuse/pyautofuse.so\n/stage/header.h\n"
    )
    assert runtime.packaged_libraries(tmp_path) == {"libascir.so", "pyautofuse.so"}
