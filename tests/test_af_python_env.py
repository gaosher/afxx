from afxx import common as workflow_common
from afxx import config as workflow_config
from afxx import execution as workflow_execution
from afxx.environment import runtime as workflow_runtime

import io
import json
from pathlib import Path
import sys
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from afxx.environment import python as e


@pytest.fixture
def env_cfg(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(e.bootstrap, "check_python", lambda *a, **k: {"passed": True})
    (tmp_path / "uv").symlink_to(sys.executable)
    cfg["python_env"] = {
        "uv_bin": str(tmp_path / "uv"),
        "base_python": sys.executable,
        "python_dir": str(tmp_path / "python"),
        "state_dir": str(tmp_path / "env-state"),
        "index_url": None,
        "torch_index_url": None,
        "stable_requirements": ["torch==2.10.0+cpu", "build", "setuptools>=68,<81"],
        "torch_npu": {
            "version": "2.10.0",
            "date": "20261112",
            "url_template": "https://packages.example.invalid/{version}/{date}/{python_tag}.tar.gz",
            "sha256": None,
        },
        "afx": {
            "wheel": str(tmp_path / "autofusex-0.1.2-py3-none-any.whl"),
            "sha256": None,
        },
    }
    wheel(Path(cfg["python_env"]["afx"]["wheel"]), "autofusex", "0.1.2")
    return cfg


def wheel(path, name, version):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            name + "-" + version + ".dist-info/METADATA",
            f"Name: {name}\nVersion: {version}\nProvides-Extra: execution\n",
        )


def test_init_discovers_remote_tools(env_cfg, monkeypatch):
    env_cfg["python_env"].update(base_python=None, uv_bin=None)
    monkeypatch.setattr(e.shutil, "which", lambda name: "/fixture/bin/" + name)
    cfg = e.validate(env_cfg, "init", None)
    assert cfg["base_python"] == "/fixture/bin/" + sys.executable
    assert cfg["uv_bin"] == str(Path(env_cfg["runtime"]["venv_dir"]) / "bin/uv")


def test_validate_sources_and_stable_component_boundary(env_cfg):
    assert e.validate(env_cfg, "init", None)
    env_cfg["python_env"]["stable_requirements"].append("torch_npu==2.10.0")
    with pytest.raises(ValueError, match="frequently updated"):
        e.validate(env_cfg, "init", None)


@pytest.mark.parametrize(
    "value",
    [
        "http://example.invalid/a",
        "https://user:secret@example.invalid/a",
        "https://example.invalid/a?token=x",
    ],
)
def test_reject_credential_or_insecure_url(value):
    with pytest.raises(ValueError):
        e.url(value)


def test_checksum_and_distribution_validation(env_cfg, tmp_path):
    path = Path(env_cfg["python_env"]["afx"]["wheel"])
    with pytest.raises(ValueError, match="checksum"):
        e.fetch(str(path), tmp_path / "copy.whl", "0" * 64)
    with pytest.raises(ValueError, match="unexpected wheel"):
        e.wheel_info(path, "torch-npu")


def test_install_never_overwrites_existing_venv(env_cfg):
    root = Path(env_cfg["runtime"]["venv_dir"])
    root.mkdir()
    (root / "keep").write_text("existing")
    with pytest.raises(ValueError, match="nonexistent"):
        e.install(env_cfg, root.parent)
    assert (root / "keep").read_text() == "existing"


def test_daily_extracts_only_selected_wheel(env_cfg, tmp_path, monkeypatch):
    path = tmp_path / "torch_npu-2.10.0-py3-none-any.whl"
    wheel(path, "torch_npu", "2.10.0")
    archive = tmp_path / "daily.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(path, arcname="../../" + path.name)
        content = b"not extracted"
        member = tarfile.TarInfo("../../escape")
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    env_cfg["python_env"]["torch_npu"]["url_template"] = str(archive)
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **k: "py311")
    monkeypatch.setattr(e, "compatible_wheels", lambda c, names: names)
    job = tmp_path / "job"
    job.mkdir()
    result = e.artifact(env_cfg, "torch_npu", job)
    assert (
        Path(result["path"]).parent
        == Path(env_cfg["python_env"]["state_dir"]) / "wheels" / result["sha256"]
    )
    assert result["source"]["sha256"] == workflow_common.sha(archive)
    assert not list(job.glob("*.whl"))
    assert not list(job.glob("*.tar.gz"))
    assert not (tmp_path.parent / "escape").exists()
    assert result["metadata"]["name"] == "torch-npu"
    again = e.artifact(env_cfg, "torch_npu", tmp_path / "second-job")
    assert again == result
    assert list(
        (Path(env_cfg["python_env"]["state_dir"]) / "wheels").rglob("*.whl")
    ) == [Path(result["path"])]


def managed(env_cfg, tmp_path):
    root = Path(env_cfg["runtime"]["venv_dir"])
    root.mkdir()
    old = tmp_path / "old.whl"
    old.write_bytes(b"old wheel")
    lock = tmp_path / "old-pylock.toml"
    lock.write_text(
        'lock-version = "1.0"\n[[packages]]\nname = "torch"\nversion = "2.10.0"\n'
    )
    state = {
        "state": "ready",
        "manager": "uv",
        "runtime": env_cfg["runtime"],
        "connection": env_cfg["connection"],
        "stable_requirements": env_cfg["python_env"]["stable_requirements"],
        "cann": {},
        "cann_config": {},
        "lock": {"path": str(lock), "sha256": workflow_common.sha(lock)},
        "packages": {"torch": "2.10.0", "torch-npu": "old", "autofusex": "old"},
        "artifacts": {
            name: {
                "path": str(old),
                "sha256": workflow_common.sha(old),
                "source": dict(env_cfg["python_env"][name]),
            }
            for name in ("torch_npu", "afx")
        },
    }
    workflow_common.write(e.state_path(env_cfg), state)
    job = tmp_path / "job"
    job.mkdir()
    return state, job


@pytest.mark.parametrize("target", ["torch_npu", "afx"])
@pytest.mark.parametrize("failure", [False, True])
def test_update_only_selected_component_and_rollback(
    env_cfg, tmp_path, monkeypatch, target, failure
):
    before, job = managed(env_cfg, tmp_path)
    inventory = dict(before["packages"])
    candidate = {"path": str(tmp_path / "new.whl"), "metadata": {}, "sha256": "new"}
    monkeypatch.setattr(e, "packages", lambda c: dict(inventory))
    monkeypatch.setattr(e, "artifact", lambda *a: candidate)
    new_lock = job / "pylock.toml"
    new_lock.write_text(
        'lock-version = "1.0"\n[[packages]]\nname = "torch"\nversion = "2.10.0"\n'
    )
    monkeypatch.setattr(e, "resolve", lambda *a, **k: new_lock)
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **k: "uv fixture")
    name = "torch-npu" if target == "torch_npu" else "autofusex"
    calls = []

    def apply(c, locked, job, component=None):
        calls.append(str(locked))
        assert component == [name]
        inventory[name] = "new" if locked == new_lock else "old"

    monkeypatch.setattr(e, "sync", apply)

    def verify(*a):
        if failure and inventory[name] == "new":
            raise ValueError("new component fails verification")

    monkeypatch.setattr(e, "verify", verify)
    if failure:
        with pytest.raises(ValueError, match="fails verification"):
            e.update(env_cfg, job, target)
        assert inventory == before["packages"]
        assert (
            workflow_common.read(job / "result.json")["rollback"]["state"] == "restored"
        )
    else:
        e.update(env_cfg, job, target)
        assert inventory[name] == "new"
        assert len(calls) == 1
    assert inventory["torch"] == "2.10.0"
    assert workflow_common.read(e.state_path(env_cfg))["state"] == "ready"


def test_dependency_mismatch_does_not_mutate(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    monkeypatch.setattr(e, "packages", lambda c: before["packages"])
    monkeypatch.setattr(e, "artifact", lambda *a: {})

    def fail(*a):
        raise ValueError("dependency mismatch")

    monkeypatch.setattr(e, "resolve", lambda *a, **k: fail())
    monkeypatch.setattr(e, "sync", lambda *a: pytest.fail("sync must not run"))
    with pytest.raises(ValueError, match="dependency mismatch"):
        e.update(env_cfg, job, "afx")
    assert workflow_common.read(e.state_path(env_cfg)) == before


@pytest.mark.parametrize("failure", [False, True])
def test_new_install_creates_ready_or_invalid_environment(
    env_cfg, tmp_path, monkeypatch, failure
):
    job = tmp_path / "install-job"
    job.mkdir()
    inventory = {"torch": "2.10.0"}
    monkeypatch.setattr(
        e.af_cann_env,
        "ensure",
        lambda c, job: workflow_common.update_record(
            job / "result.json",
            cann={"installed_version": "9.3.0", "installed_release": "202611181234"},
        ),
    )

    def create(c, args, job):
        if args[0] == "venv":
            Path(env_cfg["runtime"]["venv_dir"]).mkdir(parents=True)

    monkeypatch.setattr(workflow_common, "run", lambda *a, **k: None)
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **k: "uv fixture")
    monkeypatch.setattr(e, "uv", create)
    monkeypatch.setattr(e, "packages", lambda c: dict(inventory))
    monkeypatch.setattr(
        e, "artifact", lambda c, name, job: {"path": str(job / (name + ".whl"))}
    )
    locked = job / "pylock.toml"
    locked.write_text(
        'lock-version = "1.0"\n[[packages]]\nname = "torch"\nversion = "2.10.0"\n'
    )
    monkeypatch.setattr(e, "resolve", lambda *a, **k: locked)

    def apply(*a):
        inventory.update({"torch-npu": "daily", "autofusex": "afx"})

    monkeypatch.setattr(e, "sync", apply)

    def verify(*a, **kwargs):
        if failure:
            raise ValueError("dependency probe failed")

    monkeypatch.setattr(e, "verify", verify)
    if failure:
        with pytest.raises(ValueError, match="probe failed"):
            e.install(env_cfg, job)
    else:
        e.install(env_cfg, job)
    state = workflow_common.read(e.state_path(env_cfg))
    assert state["state"] == ("invalid" if failure else "ready")
    assert locked.is_file()


def test_latest_daily_skips_only_unpublished_dates(env_cfg, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    dates = []

    def open_url(request, timeout):
        dates.append(request.full_url)
        if "20261116" in request.full_url:
            raise e.urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return contextlib.nullcontext(SimpleNamespace(status=200))

    monkeypatch.setattr(e.urllib.request, "urlopen", open_url)
    daily = dict(env_cfg["python_env"]["torch_npu"], date="latest")
    assert e.selected_daily(daily, "py311", e.datetime.date(2026, 11, 16)) == "20261115"
    assert len(dates) == 2


def test_latest_daily_does_not_hide_access_errors(env_cfg, monkeypatch):
    def denied(request, timeout):
        raise e.urllib.error.HTTPError(request.full_url, 403, "denied", {}, None)

    monkeypatch.setattr(e.urllib.request, "urlopen", denied)
    with pytest.raises(RuntimeError, match="403"):
        e.selected_daily(
            dict(env_cfg["python_env"]["torch_npu"], date="latest"), "py311"
        )


def test_uv_sync_targets_configured_interpreter_and_full_lock(
    env_cfg, tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(e, "uv", lambda c, args, job: calls.append(args))
    locked = tmp_path / "pylock.toml"
    e.sync(env_cfg, locked, tmp_path, ["torch-npu"])
    assert calls == [
        [
            "pip",
            "sync",
            str(locked),
            "--python",
            workflow_runtime.python(env_cfg),
            "--reinstall-package",
            "torch-npu",
        ]
    ]


def test_resolve_rejects_stable_package_drift(env_cfg, tmp_path, monkeypatch):
    old = tmp_path / "old.toml"
    old.write_text('[[packages]]\nname="torch"\nversion="2.10.0"\n')
    job = tmp_path / "resolve"
    job.mkdir()

    def compile(c, args, job):
        Path(args[args.index("--output-file") + 1]).write_text(
            '[[packages]]\nname="torch"\nversion="2.11.0"\n'
        )

    monkeypatch.setattr(e, "uv", compile)
    artifacts = {
        name: {"path": str(tmp_path / (name + ".whl"))} for name in ("torch_npu", "afx")
    }
    with pytest.raises(ValueError, match="stable dependencies"):
        e.resolve(env_cfg, artifacts, job, previous=old, changed=["autofusex"])
    assert (job / "stable-constraints.txt").read_text() == "torch==2.10.0\n"
    project = e.tomllib.loads((job / "pyproject.toml").read_text())
    assert any("torch-npu @" in x for x in project["project"]["dependencies"])
    assert any(
        "autofusex[execution] @" in x for x in project["project"]["dependencies"]
    )


def test_daily_failure_retains_selection_in_result(env_cfg, tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **kw: "py311")
    monkeypatch.setattr(
        e, "fetch", lambda *a: (_ for _ in ()).throw(OSError("download failed"))
    )
    with pytest.raises(OSError, match="download failed"):
        e.artifact(env_cfg, "torch_npu", job)
    result = workflow_common.read(job / "result.json")
    assert result["daily_selection"]["date"] == "20261112"
    assert result["daily_selection"]["python_tag"] == "py311"
    assert not (job / "daily-selection.json").exists()


def test_verify_keeps_primary_and_rollback_runtime(env_cfg, tmp_path, monkeypatch):
    job = tmp_path / "job"
    job.mkdir()
    workflow_common.update_record(
        job / "result.json", error="original failure", rollback={"state": "running"}
    )
    monkeypatch.setattr(e, "uv", lambda *a: None)
    monkeypatch.setattr(workflow_runtime, "venv_preflight", lambda *a: None)
    monkeypatch.setattr(workflow_runtime, "environment", lambda *a: {})
    monkeypatch.setattr(workflow_runtime, "registry", lambda c: job / "registry")
    (job / "registry.json").touch()
    evidence = iter([{"path": "new-library"}, {"path": "restored-library"}])
    monkeypatch.setattr(workflow_runtime, "probe", lambda *a: next(evidence))
    e.verify(env_cfg, job)
    e.verify(env_cfg, job, True)
    result = workflow_common.read(job / "result.json")
    assert result["runtime"]["path"] == "new-library"
    assert result["rollback"]["runtime"]["path"] == "restored-library"
    assert result["error"] == "original failure"
    assert not (job / "runtime.json").exists()


def test_failed_rollback_preserves_original_error(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    monkeypatch.setattr(e, "packages", lambda c: before["packages"])
    monkeypatch.setattr(e, "artifact", lambda *a: {"path": "/new.whl"})
    new_lock = job / "pylock.toml"
    monkeypatch.setattr(e, "resolve", lambda *a, **kw: new_lock)

    def sync(c, locked, *args):
        if locked != new_lock:
            raise OSError("rollback failed")
        raise ValueError("original install failure")

    monkeypatch.setattr(e, "sync", sync)
    with pytest.raises(ValueError, match="original install failure"):
        e.update(env_cfg, job, "afx")
    result = workflow_common.read(job / "result.json")
    assert result["error"] == "original install failure"
    assert (
        result["rollback"]["state"] == "failed"
        and result["rollback"]["error"] == "rollback failed"
    )
    assert workflow_common.read(e.state_path(env_cfg))["state"] == "invalid"
    assert not (job / "rollback.json").exists()


def test_main_failure_keeps_all_stage_evidence(env_cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["af_python_env.py", "init", "--config", "config.yaml"]
    )
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(workflow_config, "config", lambda _: env_cfg)
    monkeypatch.setattr(e, "validate", lambda *a: env_cfg["python_env"])

    def install(c, job):
        workflow_common.update_record(
            job / "result.json",
            daily_selection={"date": "20261112"},
            runtime={"path": "library"},
            rollback={"state": "restored"},
        )
        raise ValueError("original failure")

    monkeypatch.setattr(e, "install", install)
    with pytest.raises(ValueError, match="original failure"):
        e.main()
    result_path = next(Path(env_cfg["remote"]["state_dir"]).glob("*/result.json"))
    result = workflow_common.read(result_path)
    assert result["state"] == "failed" and result["mode"] == "init"
    assert result["error"] == "original failure"
    assert result["daily_selection"]["date"] == "20261112"
    assert (
        result["runtime"]["path"] == "library"
        and result["rollback"]["state"] == "restored"
    )


def test_managed_uv_bootstraps_before_first_uv_command(env_cfg, tmp_path, monkeypatch):
    target = Path(env_cfg["runtime"]["venv_dir"])
    env_cfg["python_env"]["uv_bin"] = None
    e.validate(env_cfg, "init", None)
    monkeypatch.setattr(e.af_cann_env, "ensure", lambda *args: None)
    commands = []

    def run(argv, **kwargs):
        commands.append(argv)
        if "venv" in argv:
            target.mkdir(parents=True)

    monkeypatch.setattr(workflow_common, "run", run)

    def first_uv(c, args, job):
        assert Path(c["python_env"]["uv_bin"]) == target / "bin/uv"
        assert commands[-2][-2:] == ["venv", str(target)]
        assert commands[-1] == [
            str(target / "bin/python"),
            "-I",
            "-m",
            "pip",
            "install",
            e.MANAGED_UV,
        ]
        raise RuntimeError("reached managed uv")

    monkeypatch.setattr(e, "uv", first_uv)
    with pytest.raises(RuntimeError, match="reached managed uv"):
        e.install(env_cfg, tmp_path)
    assert workflow_common.read(e.state_path(env_cfg))["state"] == "invalid"


@pytest.mark.parametrize(
    "targets", [(), ("afx",), ("torch_npu",), ("torch_npu", "afx")]
)
def test_config_update_selects_changed_components(
    env_cfg, tmp_path, monkeypatch, targets
):
    before, job = managed(env_cfg, tmp_path)
    p = env_cfg["python_env"]
    p["torch_npu"]["date"] = "latest"
    before["artifacts"]["afx"]["sha256"] = workflow_common.sha(p["afx"]["wheel"])
    if "afx" in targets:
        Path(p["afx"]["wheel"]).write_bytes(b"new wheel")
    monkeypatch.setattr(
        e,
        "selected_daily",
        lambda *args: "20261113" if "torch_npu" in targets else "20261112",
    )
    monkeypatch.setattr(
        e, "artifact", lambda cfg, name, job: {"source": cfg["python_env"][name]}
    )
    plan = e.planned_components(env_cfg, before, job)
    assert tuple(name for name, _ in plan) == targets
    if "torch_npu" in targets:
        assert dict(plan)["torch_npu"]["source"]["date"] == "20261113"
    assert p["torch_npu"]["date"] == "latest"


def test_job_yaml_restores_without_previous_job_or_dependency_resolution(
    env_cfg, tmp_path, monkeypatch
):
    import shutil

    p = env_cfg["python_env"]
    daily = dict(p["torch_npu"], date="20261112", sha256="a" * 64)
    p["torch_npu"]["date"] = "latest"
    cached = Path(p["state_dir"]) / "wheels"
    artifacts = {}
    versions = {"torch": "2.10.0+cpu", "autofusex": "0.1.2", "torch-npu": "2.10.0"}
    for name, dist, source in [
        ("afx", "autofusex", p["afx"]),
        ("torch_npu", "torch-npu", daily),
    ]:
        path = Path(p["afx"]["wheel"]) if name == "afx" else tmp_path / "daily.whl"
        if name == "torch_npu":
            path.write_bytes(b"daily wheel")
        digest = workflow_common.sha(path)
        dest = cached / digest / path.name
        dest.parent.mkdir(parents=True)
        shutil.copyfile(path, dest)
        artifacts[name] = dict(
            path=str(dest),
            sha256=digest,
            metadata={"version": versions[dist]},
            source=source,
        )
    cann = {
        "version": "9.3.0",
        "release": "2026111201",
        "date": "20261112",
        "packages": [],
        "artifacts": [],
        "hardware": [],
    }
    root = Path(env_cfg["runtime"]["install_root"]) / "cann"
    (root / "compiler").mkdir(parents=True)
    (root / "compiler/version.info").write_text("Version=9.3.0\ntimestamp=20261112_01")
    (root / "set_env.sh").touch()
    venv = Path(env_cfg["runtime"]["venv_dir"])
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").touch()
    locked = tmp_path / "original-lock"
    locked.write_text('lock-version = "1.0"\n')
    monkeypatch.setattr(e, "packages", lambda c: versions)

    def capture(argv, **kwargs):
        if "--version" in argv:
            return "Python 3.11.4"
        data = {}
        for name, version in versions.items():
            component = {"torch-npu": "torch_npu", "autofusex": "afx"}.get(name)
            origin = Path(artifacts[component]["path"]).as_uri() if component else None
            data[name] = [version, version, str(venv / name), origin]
        return json.dumps([str(venv), "Python 3.11.4", versions, data])

    monkeypatch.setattr(workflow_common, "capture", capture)
    monkeypatch.setattr(workflow_runtime, "environment", lambda c: {})
    manifest = e.ready(env_cfg, artifacts, locked, cann, {"date": cann["date"]})
    workflow_common.write(e.state_path(env_cfg), manifest)
    job = tmp_path / "replay"
    job.mkdir()
    workflow_execution.save_frozen_config(env_cfg, job, manifest)
    assert [path.name for path in job.glob("config.*")] == ["config.yaml"]
    frozen = workflow_config.config(job / "config.yaml")
    assert frozen["python_env"]["torch_npu"] == daily
    assert frozen["cann"]["date"] == "20261112"
    versions["autofusex"] = "0.1.2"
    locked.unlink()
    e.state_path(env_cfg).unlink()
    shutil.rmtree(venv)
    monkeypatch.setattr(
        e.af_cann_env,
        "ensure",
        lambda c, j: workflow_common.update_record(
            j / "result.json",
            cann=dict(installed_version="9.3.0", installed_release=cann["release"]),
        ),
    )
    monkeypatch.setattr(workflow_common, "run", lambda *a, **kw: None)
    monkeypatch.setattr(e, "uv", lambda *args: None)
    monkeypatch.setattr(e, "verify", lambda *args, **kw: None)
    monkeypatch.setattr(e, "artifact", lambda c, name, j: artifacts[name])
    monkeypatch.setattr(
        e, "resolve", lambda *args: pytest.fail("must use embedded lock")
    )
    synchronized = []
    monkeypatch.setattr(
        e, "sync", lambda c, lock, j: synchronized.append(lock.read_text())
    )
    e.install(frozen, tmp_path / "restore")
    assert synchronized == [frozen["environment_lock"]["pylock"]]
    assert workflow_common.read(e.state_path(frozen))["packages"] == versions
    with pytest.raises(ValueError, match="hash mismatch"):
        e.restore_lock(frozen, dict(frozen["environment_lock"], pylock="tampered"))


@pytest.mark.parametrize("requested", ["0.2.0", "latest"])
def test_afx_version_download_freeze_and_update(
    env_cfg, tmp_path, monkeypatch, requested
):
    p = env_cfg["python_env"]
    before, job = managed(env_cfg, tmp_path)
    p["afx"] = {"version": requested, "wheel": None, "sha256": None}
    e.validate(env_cfg, "update", "afx")

    def download(argv, **kwargs):
        assert argv[-1] == (
            "autofusex" if requested == "latest" else "autofusex==0.2.0"
        )
        assert "--no-deps" in argv and "--only-binary=:all:" in argv
        wheel(
            Path(argv[argv.index("--dest") + 1]) / "autofusex-0.2.0-py3-none-any.whl",
            "autofusex",
            "0.2.0",
        )

    monkeypatch.setattr(workflow_common, "run", download)
    monkeypatch.setattr(e, "compatible_wheels", lambda c, names: names)
    plan = e.planned_components(env_cfg, before, job)
    assert [name for name, _ in plan] == ["afx"]
    record = dict(plan)["afx"]
    assert record["metadata"]["version"] == "0.2.0"
    assert record["source"]["version"] == "0.2.0"
    assert record["sha256"] == workflow_common.sha(record["path"])
    before["artifacts"]["afx"] = record
    assert e.planned_components(env_cfg, before, job) == []
    p["afx"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum mismatch"):
        e.artifact(env_cfg, "afx", job)
    p["afx"]["wheel"] = record["path"]
    with pytest.raises(ValueError, match="exactly one"):
        e.validate(env_cfg, "update", "afx")
