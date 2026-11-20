from afxx import common as workflow_common

import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from afxx.environment import cann as e


def test_existing_skip_and_failed_install_block(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "cann/compiler").mkdir(parents=True)
    (root / "cann/set_env.sh").touch()
    (root / "cann/compiler/version.info").touch()
    c = {"runtime": {"install_root": str(root)}}
    monkeypatch.setattr(e, "listing", lambda _: pytest.fail("must not download"))
    e.ensure(c, tmp_path)
    assert workflow_common.read(tmp_path / "result.json")["cann"]["state"] == "existing"
    workflow_common.write(root / ".af-cann-bootstrap.json", {"state": "invalid"})
    with pytest.raises(ValueError, match="previous CANN"):
        e.ensure(c, tmp_path)


def test_select_latest_complete_same_version(monkeypatch):
    cfg = {"repository_url": "https://fixture.invalid/", "nnal": False}
    pages = {
        "https://fixture.invalid/": ["20261116/", "20261115/"],
        "https://fixture.invalid/20261116/": [
            "Ascend-cann-toolkit_9.3_linux-aarch64.run"
        ],
        "https://fixture.invalid/20261115/": [
            "Ascend-cann-toolkit_9.2_linux-aarch64.run",
            "Ascend-cann-A3-ops_9.2_linux-aarch64.run",
        ],
    }
    monkeypatch.setattr(e, "listing", lambda url: pages[url])
    result = e.select_release(cfg, "A3", "aarch64")
    assert result["release"] == "20261115" and result["skipped_incomplete"] == [
        "20261116"
    ]
    cfg["nnal"] = True
    with pytest.raises(ValueError, match="no complete"):
        e.select_release(cfg, "A3", "aarch64")


def test_hardware_mapping_unknown_and_mixed(monkeypatch):
    cfg = {"driver_library_dirs": [], "ops_compatibility": {"Chip_123": "family"}}

    def capture(args, **kw):
        if "-m" in args:
            return "2 0 4 x\n2 1 5 x"
        return "Chip Name : Chip\nNPU Name : " + ("123" if args[-1] == "0" else "456")

    monkeypatch.setattr(workflow_common, "capture", capture)
    with pytest.raises(ValueError, match=r"Chip_456.*cann\.ops_compatibility"):
        e.detect_ops(cfg)
    cfg["ops_compatibility"]["Chip_456"] = "other"
    with pytest.raises(ValueError, match="mixed OPS"):
        e.detect_ops(cfg)
    cfg["ops_compatibility"]["Chip_456"] = "family"
    family, evidence = e.detect_ops(cfg)
    assert family == "family" and len(evidence) == 2


@pytest.mark.parametrize("entry", ["cann", "unrelated"])
def test_partial_install_refused(tmp_path, entry):
    (tmp_path / entry).mkdir()
    c = {
        "runtime": {"install_root": str(tmp_path)},
        "cann": {
            "auto_install": True,
            "repository_url": "https://fixture.invalid/",
            "ops": "auto",
            "nnal": False,
            "ops_compatibility": {},
            "driver_library_dirs": [],
        },
    }
    with pytest.raises(ValueError, match="partial"):
        e.ensure(c, tmp_path)


@pytest.mark.parametrize("failure", [None, "install", "download"])
def test_cann_install_cleans_only_successful_downloads(tmp_path, monkeypatch, failure):
    import io
    import json
    import subprocess

    fail_install = failure == "install"
    root = tmp_path / "ascend"
    job = tmp_path / "job"
    job.mkdir()
    workflow_common.write(job / "result.json", {"base_python": {"path": "/existing/python"}})
    cfg = {
        "auto_install": True,
        "repository_url": "https://fixture.invalid/",
        "ops": "auto",
        "nnal": False,
        "ops_compatibility": {},
        "driver_library_dirs": [],
    }
    c = {"runtime": {"install_root": str(root)}, "cann": cfg}
    monkeypatch.setattr(e, "detect_ops", lambda _: ("family", []))
    monkeypatch.setattr(
        e,
        "select_release",
        lambda *a: {
            "base_url": "https://fixture.invalid/",
            "packages": [("toolkit", "toolkit.run"), ("ops", "ops.run")],
            "version": "test",
        },
    )
    temporary = tmp_path / "downloads"

    def mkdtemp(*, prefix, dir):
        assert dir == "/tmp" and prefix.startswith("af-cann-" + job.name + "-")
        temporary.mkdir(mode=0o700)
        return str(temporary)

    monkeypatch.setattr(e.tempfile, "mkdtemp", mkdtemp)

    def download(url, **kw):
        checkpoint = workflow_common.read(job / "result.json")["cann"]
        assert checkpoint["download_dir"] == str(temporary)
        if failure == "download":
            raise OSError("download interrupted")
        stream = io.BytesIO(b"fake installer")
        stream.url = url
        return stream

    monkeypatch.setattr(e.urllib.request, "urlopen", download)

    def run(argv, **kw):
        if "--check" in argv:
            return
        (temporary / "install.log").write_text("preserve installer log")
        if "toolkit.run" in str(argv[1]):
            assert "--full" in argv and "--pylocal" not in argv
            (root / "cann/compiler").mkdir(parents=True)
            (root / "cann/set_env.sh").touch()
            (root / "cann/compiler/version.info").touch()
        elif fail_install:
            raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(workflow_common, "run", run)
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **kw: "{}")
    if failure == "download":
        with pytest.raises(OSError, match="download interrupted"):
            e.ensure(c, job)
        report = workflow_common.read(job / "result.json")["cann"]
        assert report["state"] == "download_failed"
        assert report["download_dir"] == str(temporary) and temporary.is_dir()
        assert not (root / ".af-cann-bootstrap.json").exists()
        return
    if fail_install:
        with pytest.raises(subprocess.CalledProcessError):
            e.ensure(c, job)
    else:
        e.ensure(c, job)
    assert workflow_common.read(job / "result.json")["base_python"]["path"] == "/existing/python"
    report = json.loads((job / "result.json").read_text())["cann"]
    assert (temporary / "install.log").is_file()
    assert (temporary / "toolkit.run").exists() == fail_install
    assert (temporary / "ops.run").exists() == fail_install
    assert report["download_dir"] == str(temporary)
    assert not (job / "cann-packages").exists()
    assert report["state"] == ("invalid" if fail_install else "ready")
    assert all(item["sha256"] for item in report["artifacts"])
    if not fail_install:
        assert report["cleanup"]["state"] == "complete"
    assert e.ready(root)


def test_cleanup_failure_does_not_invalidate_installed_cann(tmp_path, monkeypatch):
    path = tmp_path / "package.run"
    path.touch()
    report = {"state": "ready"}
    monkeypatch.setattr(
        Path, "unlink", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("busy"))
    )
    e.cleanup_packages(tmp_path, [{"path": str(path)}], report)
    assert report["state"] == "ready" and report["cleanup"]["state"] == "failed"
    assert path.exists()


def test_cleanup_removes_empty_package_directory(tmp_path):
    directory = tmp_path / "cann-packages"
    directory.mkdir()
    path = directory / "toolkit.run"
    path.touch()
    report = {"state": "ready"}
    e.cleanup_packages(directory, [{"path": str(path)}], report)
    assert not directory.exists()
    assert report["cleanup"]["state"] == "complete"


def test_pinned_date_selects_latest_complete_build_only_on_that_day(monkeypatch):
    root = "https://fixture.invalid/"
    releases = [
        "20261115000000000",
        "20261116010000000",
        "20261116020000000",
        "20261117000000000",
    ]
    seen = []

    def listing(url):
        if url == root:
            return [release + "/" for release in releases]
        release = url.rstrip("/").split("/")[-1]
        seen.append(release)
        return (
            []
            if release == releases[2]
            else ["Ascend-cann-toolkit_9.2_linux-aarch64.run"]
        )

    monkeypatch.setattr(e, "listing", listing)
    cfg = {"repository_url": root, "nnal": False, "date": "20261116"}
    assert e.select_release(cfg, None, "aarch64")["release"] == releases[1]
    assert seen == [releases[2], releases[1]]
    cfg["date"] = "20261114"
    with pytest.raises(ValueError, match="date=20261114"):
        e.select_release(cfg, None, "aarch64")


@pytest.mark.parametrize("ops", [None, "", "auto"])
def test_blank_ops_uses_hardware_mapping(ops, monkeypatch):
    evidence = [{"identity": "fixture"}]
    monkeypatch.setattr(e, "detect_ops", lambda cfg: ("A3", evidence))
    assert e.select_ops({"ops": ops}) == ("A3", evidence)


@pytest.mark.parametrize(
    ("ops", "family"), [("910b", "910b"), ("950", "950"), ("none", None)]
)
def test_explicit_ops_bypasses_detection(ops, family, monkeypatch):
    monkeypatch.setattr(
        e, "detect_ops", lambda cfg: pytest.fail("unexpected hardware probe")
    )
    assert e.select_ops({"ops": ops}) == (family, [])


@pytest.mark.parametrize("ops", [950, "../ops"])
def test_invalid_ops_override(ops):
    with pytest.raises(ValueError, match="cann.ops"):
        e.select_ops({"ops": ops})
