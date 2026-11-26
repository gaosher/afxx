"""Workflow contracts, using private directories and fake build/install commands."""

import afxx.cli
import afxx.cli as w
import os
import shutil
import subprocess
from afxx import common as workflow_common
from afxx import config as workflow_config
from afxx import execution as workflow_execution
from afxx import jobs as workflow_jobs
from afxx.environment import runtime as workflow_runtime
from afxx import source as workflow_source

import json
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from test_replay_adapter import results


@pytest.fixture
def cfg(tmp_path):
    c = {
        "schema_version": 1,
        "connection": {"environment": "test-env"},
        "local": {
            "source_dir": str(tmp_path / "local"),
            "state_dir": str(tmp_path / "local-state"),
        },
        "remote": {
            "source_dir": str(tmp_path / "remote"),
            "state_dir": str(tmp_path / "remote-state"),
        },
        "runtime": {
            "install_root": str(tmp_path / "ascend"),
            "venv_dir": str(tmp_path / "venv"),
        },
        "build": {
            "llvm_build_dir": str(tmp_path / "llvm"),
            "lld_bin": str(tmp_path / "llvm/bin/ld.lld"),
            "jobs": 8,
        },
        "sync": {"include_ignored_paths": [], "exclude_paths": []},
        "replay": {
            "results_dir": str(tmp_path / "results"),
            "devices": ["npu:0"],
            "workers": 2,
            "compile_threads": 2,
            "batch_timeout_seconds": 300,
            "dump": False,
        },
    }
    source = Path(c["local"]["source_dir"])
    source.mkdir()
    workflow_common.run(["git", "init", "-q", source])
    (source / "build.sh").write_text("#!/bin/bash\nexit 0\n")
    (source / "old.cpp").write_text("old")
    (source / ".gitignore").write_text("build/\nignored.cpp\n")
    workflow_common.run(["git", "-C", source, "add", "."])
    workflow_common.run(
        [
            "git",
            "-C",
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ]
    )
    return c


def test_config_rejects_unknown_null_overlap_and_bad_budget(cfg, tmp_path):
    p = tmp_path / "config.yaml"
    workflow_common.write(p, cfg)
    assert workflow_config.config(p) == cfg
    for section, key, value in [
        ("build", "jobs", 0),
        ("build", "jobs", -1),
        ("build", "jobs", True),
        ("build", "jobs", 1.5),
        ("build", "lld_bin", None),
        ("local", "state_dir", cfg["local"]["source_dir"]),
        ("replay", "devices", ["npu:0", "npu:0"]),
        ("replay", "workers", True),
    ]:
        changed = json.loads(json.dumps(cfg))
        changed[section][key] = value
        workflow_common.write(p, changed)
        with pytest.raises(ValueError):
            workflow_config.config(p)
    p.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="duplicate"):
        workflow_config.config(p)


@pytest.mark.parametrize("jobs", [1, 40])
def test_config_accepts_positive_build_parallelism(cfg, tmp_path, jobs):
    cfg["build"]["jobs"] = jobs
    p = tmp_path / "config.yaml"
    workflow_common.write(p, cfg)
    assert workflow_config.config(p)["build"]["jobs"] == jobs


def test_snapshot_includes_dirty_new_and_deletion_excludes_private(cfg, tmp_path):
    source = Path(cfg["local"]["source_dir"])
    (source / "old.cpp").unlink()
    (source / "new.cpp").write_text("new")
    (source / "build.sh").write_text("changed")
    (source / ".env").write_text("private")
    (source / "ignored.cpp").write_text("ignore")
    (source / "build").mkdir()
    (source / "build/cache").write_text("cache")
    dest = tmp_path / "snapshot"
    result = workflow_source.snapshot(cfg, dest)
    assert "new.cpp" in result["files"]
    assert "old.cpp" not in result["files"]
    assert ".env" not in result["files"]
    assert "ignored.cpp" not in result["files"]
    assert "build/cache" not in result["files"]
    assert (dest / "build.sh").read_text() == "changed"
    workflow_source.verify(dest, result)
    (dest / "new.cpp").write_text("corrupt")
    with pytest.raises(ValueError, match="checksum"):
        workflow_source.verify(dest, result)


def test_snapshot_rejects_external_symlink(cfg, tmp_path):
    (Path(cfg["local"]["source_dir"]) / "external").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="external"):
        workflow_source.snapshot(cfg, tmp_path / "snapshot")


def job_snapshot(cfg, directory):
    directory.mkdir()
    workflow_common.write(
        directory / "source.json", workflow_source.snapshot(cfg, directory / "snapshot")
    )
    return directory


def test_sync_deletes_managed_files_preserves_build(cfg, tmp_path):
    job1 = job_snapshot(cfg, tmp_path / "job1")
    source, _ = workflow_source.sync_source(cfg, job1)
    (source / "build").mkdir()
    (source / "build/cache").write_text("keep")
    local = Path(cfg["local"]["source_dir"])
    (local / "old.cpp").unlink()
    (local / "new.cpp").write_text("replacement")
    job2 = job_snapshot(cfg, tmp_path / "job2")
    workflow_source.sync_source(cfg, job2)
    assert not (source / "old.cpp").exists()
    assert (source / "new.cpp").read_text() == "replacement"
    assert (source / "build/cache").read_text() == "keep"


def test_sync_refuses_unmanaged_directory(cfg, tmp_path):
    source = Path(cfg["remote"]["source_dir"])
    source.mkdir()
    (source / "user-work").write_text("keep")
    with pytest.raises(ValueError, match="unmanaged"):
        workflow_source.sync_source(cfg, job_snapshot(cfg, tmp_path / "job"))
    assert (source / "user-work").read_text() == "keep"


def test_sync_refuses_remote_user_edits(cfg, tmp_path):
    job = job_snapshot(cfg, tmp_path / "job")
    source, _ = workflow_source.sync_source(cfg, job)
    (source / "old.cpp").write_text("remote edit")
    with pytest.raises(ValueError, match="checksum"):
        workflow_source.sync_source(cfg, job)


def test_build_failure_does_not_install(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_execution, "record_environment", lambda c, job: None)
    job = job_snapshot(cfg, tmp_path / "job")
    retry = job_snapshot(cfg, tmp_path / "retry")
    monkeypatch.setattr(workflow_runtime, "environment", lambda c: {})
    monkeypatch.setattr(workflow_runtime, "venv_preflight", lambda *a: {})
    monkeypatch.setattr(workflow_runtime, "registry", lambda c: tmp_path / "registry")
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **k: "Python fixture")
    original = workflow_common.run
    commands = []

    def fake_run(argv, **kwargs):
        commands.append([str(a) for a in argv])
        if "build.sh" in argv:
            raise subprocess.CalledProcessError(1, argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(workflow_common, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        workflow_execution.deploy(cfg, job)
    stamp = Path(cfg["remote"]["source_dir"]) / "build/.workflow-environment.json"
    assert not stamp.exists()
    cached = stamp.parent / "cached-object"
    cached.write_bytes(b"existing")
    stamp.write_text("obsolete stamp")
    with pytest.raises(subprocess.CalledProcessError):
        workflow_execution.deploy(cfg, retry)
    assert not stamp.exists()
    assert cached.read_bytes() == b"existing"
    assert sum("build.sh" in command for command in commands) == 2
    assert not any("--upgrade" in a for a in commands)
    assert not (job / "result.json").exists()


def test_cli_dry_run_never_connects(cfg, tmp_path):
    p = tmp_path / "config.yaml"
    import yaml

    p.write_text(yaml.safe_dump(cfg))
    completed = subprocess.run(
        [
            sys.executable,
            "-m", "afxx.cli",
            "deploy",
            "--config",
            str(p),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["ok"]
    result = envelope["data"]
    assert Path(result["local"], "source.json").exists()
    assert Path(result["local"], "afxx/afx/replay.py").is_file()
    staged = subprocess.run(
        [sys.executable, "-m", "afxx.cli", "--help"],
        cwd=result["local"],
        capture_output=True,
        text=True,
    )
    assert staged.returncode == 0, staged.stderr
    assert not Path(result["remote"]).exists()


@pytest.mark.parametrize("failure", [None, "install", "provenance"])
@pytest.mark.parametrize("managed", [False, True])
def test_deploy_publishes_only_verified_install(
    cfg, tmp_path, monkeypatch, failure, managed
):
    monkeypatch.setattr(workflow_execution, "record_environment", lambda c, job: None)
    job = job_snapshot(cfg, tmp_path / "job")
    root = Path(cfg["runtime"]["install_root"])
    (root / "cann/compiler").mkdir(parents=True)
    (root / "cann/compiler/version.info").write_text("Version=test")
    lld = Path(cfg["build"]["lld_bin"])
    lld.parent.mkdir(parents=True)
    lld.write_text("lld")
    for package in ("llvm", "mlir"):
        path = Path(cfg["build"]["llvm_build_dir"]) / "lib/cmake" / package
        path.mkdir(parents=True)
        (path / (package.upper() + "Config.cmake")).write_text(package)
    monkeypatch.setattr(workflow_runtime, "environment", lambda c: {})
    monkeypatch.setattr(workflow_runtime, "venv_preflight", lambda *a: {})
    monkeypatch.setattr(workflow_common, "capture", lambda *a, **k: "Python fixture")
    monkeypatch.setattr(workflow_runtime, "registry", lambda c: tmp_path / "registry")
    monkeypatch.setattr(workflow_runtime, "check_idle", lambda *a, **kw: None)
    monkeypatch.setattr(workflow_runtime, "packaged_libraries", lambda *a: {"pyautofuse.so"})
    evidence = {}
    original = workflow_common.run

    def fake_run(argv, **kwargs):
        if "build.sh" in argv:
            source = Path(cfg["remote"]["source_dir"])
            for key, name in [
                ("pyautofuse", "pyautofuse.so"),
                ("adapter", "compile_adapter.py"),
                ("codegen", "libaihac_codegen.so"),
            ]:
                path = source / "build" / name
                path.write_text("fresh-" + key)
                installed = root / name
                shutil.copyfile(path, installed)
                evidence[key] = {
                    "path": str(installed),
                    "sha256": workflow_common.sha(installed),
                }
            (job / "packages/cann-graph-autofusion_test.run").write_text("package")
            return subprocess.CompletedProcess(argv, 0)
        if "--upgrade" in argv:
            if failure == "install":
                raise subprocess.CalledProcessError(1, argv)
            if failure == "provenance":
                evidence["adapter"]["sha256"] = "stale"
            return subprocess.CompletedProcess(argv, 0)
        if "--check" in argv:
            return subprocess.CompletedProcess(argv, 0)
        return original(argv, **kwargs)

    monkeypatch.setattr(workflow_common, "run", fake_run)
    monkeypatch.setattr(workflow_runtime, "probe", lambda *a: evidence)
    if managed:
        cfg["platform"] = {}
        workflow_common.write(
            job / "request.json",
            {
                "issue": {
                    "id": "F152",
                    "lease_id": "lease",
                }
            },
        )

        monkeypatch.setattr(
            "afxx.afx.platform.verify",
            lambda *a: pytest.fail("deploy must not submit verify"),
        )
    if failure:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            workflow_execution.deploy(cfg, job)
        assert workflow_common.read(tmp_path / "registry.json")["state"] == "invalid"
        assert not (job / "result.json").exists()
    else:
        workflow_execution.deploy(cfg, job)
        deployment = workflow_common.read(job / "result.json")
        assert deployment["state"] == "success"
        assert "files" not in deployment["source"]
        assert deployment["source"]["sha256"] == workflow_common.sha(
            job / "source.json"
        )
        assert set(workflow_common.read(tmp_path / "registry.json")) == {
            "state",
            "job",
            "deployment",
        }
        assert (
            workflow_runtime.current_deployment(cfg, job / "result.json")
            == deployment
        )
        assert workflow_common.read(
            tmp_path / "registry.json"
        ) == workflow_runtime.deployment_reference(job / "result.json")


def test_interrupted_sync_can_retry_and_remove_partial_files(
    cfg, tmp_path, monkeypatch
):
    job1 = job_snapshot(cfg, tmp_path / "job1")
    original = workflow_common.run

    def interrupted(argv, **kwargs):
        source = Path(cfg["remote"]["source_dir"])
        shutil.copyfile(job1 / "snapshot/old.cpp", source / "old.cpp")
        raise subprocess.CalledProcessError(23, argv)

    monkeypatch.setattr(workflow_common, "run", interrupted)
    with pytest.raises(subprocess.CalledProcessError):
        workflow_source.sync_source(cfg, job1)
    monkeypatch.setattr(workflow_common, "run", original)
    (Path(cfg["local"]["source_dir"]) / "old.cpp").unlink()
    job2 = job_snapshot(cfg, tmp_path / "job2")
    source, _ = workflow_source.sync_source(cfg, job2)
    assert not (source / "old.cpp").exists()
    assert workflow_common.read(source / ".af-workflow")["complete"] is True


def test_run_terminates_child_group_on_interrupt(monkeypatch):
    calls = []

    class Child:
        pid = 12345

        def communicate(self, timeout=None):
            raise KeyboardInterrupt()

        def wait(self, timeout=None):
            calls.append(("wait", timeout))

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(os, "killpg", lambda *a: calls.append(("kill", a)))
    with pytest.raises(KeyboardInterrupt):
        workflow_common.run(["unused"])
    assert calls[0][0] == "kill"
    assert calls[1] == ("wait", 10)


def test_remote_unmanaged_source_file_rejected(cfg, tmp_path):
    job = job_snapshot(cfg, tmp_path / "job")
    source, _ = workflow_source.sync_source(cfg, job)
    (source / "injected.cpp").write_text("untracked remote source")
    with pytest.raises(ValueError, match="unmanaged files"):
        workflow_source.sync_source(cfg, job)


def test_toolchain_parent_directory_is_allowed(cfg, tmp_path):
    cfg["build"]["llvm_build_dir"] = str(tmp_path)
    path = tmp_path / "config.yaml"
    workflow_common.write(path, cfg)
    assert workflow_config.config(path)["build"]["llvm_build_dir"] == str(tmp_path)


@pytest.mark.parametrize("failure", [None, "launch", "replay"])
def test_replay_managed_worker_routing(cfg, tmp_path, monkeypatch, failure):
    job = tmp_path / "managed-job"
    job.mkdir()
    monkeypatch.setattr(workflow_execution, "record_environment", lambda *a: {"af": {"sha": "submitter"}})
    monkeypatch.setattr(workflow_runtime, "environment", lambda *a: {"EXISTING_HOME": "retained"})
    def run(argv, **kwargs):
        assert argv == [workflow_runtime.python(cfg), "-m", "afxx.afx.fuzz_replay", str(job)]
        assert kwargs["env"] == {"EXISTING_HOME": "retained"}
        if failure == "launch":
            raise OSError("launch failed")
        workflow_common.update_record(job / "result.json", passed=failure is None,
                                     error_code="AFX_TEST_ERROR", error="replay failed")
        return subprocess.CompletedProcess(argv, 2 if failure else 0)
    monkeypatch.setattr(workflow_common, "run", run)
    if failure:
        with pytest.raises((ValueError, OSError)):
            workflow_execution.replay(cfg, job, {})
    else:
        workflow_execution.replay(cfg, job, {})
    report = workflow_common.read(job / "result.json")
    assert report["state"] == ("failed" if failure else "success")
    assert report["execution_mode"] == "fuzz_replay"
    assert "runtime" not in report  # submitting interpreter is not the execution identity
    assert "evidence" in report


def test_replay_does_not_require_deployment(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_runtime, "current_deployment", lambda *a: pytest.fail("deployment dependency"))
    def capture(*args):
        raise RuntimeError("actual environment capture reached")
    monkeypatch.setattr(workflow_execution, "record_environment", capture)
    with pytest.raises(RuntimeError, match="actual environment capture reached"):
        workflow_execution.replay(cfg, tmp_path, {"request_id": "repair", "run": 1})


def test_dependency_report_collects_import_failures_and_prefix(cfg, monkeypatch):
    monkeypatch.setattr(
        workflow_runtime,
        "dependency_checks",
        lambda *a: [
            ("present", [sys.executable, "-c", 'print("present")']),
            (
                "absent",
                [
                    sys.executable,
                    "-c",
                    "import deliberately_missing_workflow_dependency",
                ],
            ),
        ],
    )
    report = workflow_runtime.python_dependency_report(
        "autofusex", "deploy", cfg["runtime"]["venv_dir"]
    )
    checks = {c["name"]: c for c in report["checks"]}
    assert checks["present"]["passed"]
    assert not checks["absent"]["passed"]
    assert "ModuleNotFoundError" in checks["absent"]["stderr"]
    assert not checks["venv-interpreter"]["passed"]
    assert not report["passed"]


def test_dependency_scope_and_commands_compile():
    build = dict(workflow_runtime.dependency_checks("autofusex", "deploy"))
    runtime = dict(workflow_runtime.dependency_checks("autofusex", "replay"))
    assert "build" in build and "build" not in runtime
    assert "torch_npu" in runtime and "pip-check" not in runtime
    full = dict(workflow_runtime.dependency_checks("autofusex", "full"))
    assert "pip-check" in full and "build" in full and "afx-contracts" in full
    assert not {"pip-check", "nnsmith", "z3", "fuzz-cli", "afx-contracts", "nnsmith-source-and-z3", "sqlite>=3.35"} & build.keys()
    assert "nnsmith-source-and-z3" in runtime
    for command in [*build.values(), *runtime.values(), *full.values()]:
        if "-c" in command:
            compile(command[command.index("-c") + 1], "<dependency-check>", "exec")


def test_dependency_failure_report_saved(cfg, tmp_path, monkeypatch):
    report = {
        "passed": False,
        "checks": [
            {"name": "pip-check", "passed": False, "stdout": "missing dependency"}
        ],
    }
    monkeypatch.setattr(
        workflow_common,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, json.dumps(report), ""),
    )
    with pytest.raises(ValueError, match="pip-check"):
        workflow_runtime.venv_preflight(cfg, {}, tmp_path, "deploy")
    assert workflow_common.read(tmp_path / "environment.json")["dependencies"] == report


def test_dependency_failure_blocks_sync(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(workflow_execution, "record_environment", lambda c, job: None)
    monkeypatch.setattr(workflow_runtime, "registry", lambda c: tmp_path / "registry")
    monkeypatch.setattr(workflow_runtime, "environment", lambda c: {})

    def fail(*args):
        raise ValueError("dependency failure")

    monkeypatch.setattr(workflow_runtime, "venv_preflight", fail)
    monkeypatch.setattr(
        workflow_source, "sync_source", lambda *a: pytest.fail("must not sync")
    )
    with pytest.raises(ValueError, match="dependency failure"):
        workflow_execution.deploy(cfg, tmp_path)


def test_dependency_timeout_recorded(monkeypatch):
    monkeypatch.setattr(
        workflow_runtime, "dependency_checks", lambda *a: [("slow-import", ["unused"])]
    )

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("unused", 60)

    monkeypatch.setattr(workflow_common, "run", timeout)
    report = workflow_runtime.python_dependency_report(
        "autofusex", "replay", sys.prefix
    )
    assert not report["passed"]
    assert report["checks"][-1]["name"] == "slow-import"
    assert "timed out" in report["checks"][-1]["detail"]


def test_environment_does_not_inherit_other_venv_python_paths(cfg, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/other/venv/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/other/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/other/venv")

    def capture(argv, **kwargs):
        assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}.intersection(
            kwargs["env"]
        )
        return json.dumps({"PATH": "/fixture/bin", "PYTHONPATH": "/cann/helpers"})

    monkeypatch.setattr(workflow_common, "capture", capture)
    result = workflow_runtime.environment(cfg)
    assert result["PYTHONPATH"].startswith(
        cfg["runtime"]["install_root"] + "/cann/python/site-packages:"
    )
    assert "/other/" not in result["PYTHONPATH"]


def test_local_preflight_missing_tools_reports_all(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    monkeypatch.setattr(
        workflow_common,
        "run",
        lambda *a, **kw: pytest.fail("must fail before execution"),
    )
    with pytest.raises(ValueError) as error:
        workflow_jobs.local_preflight("deploy")
    for name in ("git", "node", "npussh", "ssh", "npursync", "rsync"):
        assert name in str(error.value)
    assert "npm install -g npussh" in str(error.value)


@pytest.mark.parametrize(
    "command,dry_run,expected",
    [
        ("deploy", True, {"git"}),
        ("replay", True, set()),
        ("check", True, set()),
        ("status", False, {"node", "npussh", "ssh"}),
        ("deploy", False, {"git", "node", "npussh", "ssh", "npursync", "rsync"}),
        ("replay", False, {"node", "npussh", "ssh", "npursync", "rsync"}),
        ("check", False, {"node", "npussh", "ssh", "npursync", "rsync"}),
    ],
)
def test_local_preflight_checks_only_required_tools(
    monkeypatch, command, dry_run, expected
):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stdin"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(
            argv, 0, "v20.1.0\n" if argv[0] == "node" else "", ""
        )

    monkeypatch.setattr(workflow_common, "run", run)
    workflow_jobs.local_preflight(command, dry_run)
    assert {argv[0] for argv in calls} == expected
    assert (["npussh", "list"] in calls) == (not dry_run)


@pytest.mark.parametrize(
    "failure,match",
    [
        ("尚未登录 token-secret", "npussh login"),
        ("invalid_grant token-secret", "npussh login"),
        ("HTTP 401 token-secret", "npussh login"),
        ("network error token-secret", "网络、服务"),
        ("timeout", "30 秒"),
    ],
)
def test_local_preflight_login_failure_is_safe(monkeypatch, failure, match):
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)

    def run(argv, **kwargs):
        if argv == ["npussh", "list"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 1, "", failure)
        return subprocess.CompletedProcess(argv, 0, "v20.1.0\n", "")

    monkeypatch.setattr(workflow_common, "run", run)
    with pytest.raises(ValueError, match=match) as error:
        workflow_jobs.local_preflight("check")
    assert "token-secret" not in str(error.value)


def test_local_preflight_rejects_old_node_and_status_dry_run(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        workflow_common,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "v18.0.0", ""),
    )
    with pytest.raises(ValueError, match="Node.js 20"):
        workflow_jobs.local_preflight("status")
    with pytest.raises(ValueError, match="status 不支持"):
        workflow_jobs.local_preflight("status", True)


def test_cli_preflight_failure_creates_no_task(cfg, tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    workflow_common.write(path, cfg)
    monkeypatch.setattr(
        sys, "argv", ["afxx/cli.py", "deploy", "--config", str(path)]
    )
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="缺少本地依赖"):
        afxx.cli.main()
    assert not Path(cfg["local"]["state_dir"]).exists()


@pytest.mark.parametrize("extension", ["yaml", "yml"])
def test_yaml_config_freezes_as_json(cfg, tmp_path, extension):
    import yaml

    path = tmp_path / ("config." + extension)
    path.write_text("# user comment\n" + yaml.safe_dump(cfg))
    assert workflow_config.config(path) == cfg
    frozen = tmp_path / "job/config.json"
    workflow_common.write(frozen, workflow_config.config(path))
    assert workflow_config.validate_config(workflow_common.read(frozen)) == cfg


@pytest.mark.parametrize(
    "text,match",
    [
        ("schema_version: 1\nschema_version: 1\n", "duplicate YAML key"),
        ("build:\n  jobs: 8\n  jobs: 4\n", "duplicate YAML key"),
        ("? [a, b]\n: value\n", "keys must be strings"),
        ("a: !!python/object/apply:os.system [echo unsafe]", "invalid YAML"),
        ("a: [\n", "invalid YAML"),
        ("a: 2026-11-16\n", "quote dates"),
        ("a: .nan\n", "JSON-compatible"),
        ("a: &a [*a]\n", "JSON-compatible"),
        ("a: {<<: {b: 1}}\n", "invalid YAML"),
        ("", "must be a mapping"),
        ("[]", "must be a mapping"),
    ],
)
def test_yaml_config_rejects_ambiguous_or_unsafe_values(tmp_path, text, match):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match=match):
        workflow_config.config(path)


def test_yaml_missing_dependency_does_not_break_internal_snapshot(
    cfg, tmp_path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "yaml", None)
    path = tmp_path / "config.yaml"
    path.write_text("schema_version: 1")
    with pytest.raises(ValueError, match="PyYAML") as error:
        workflow_config.config(path)
    assert sys.executable in str(error.value)
    path = tmp_path / "config.json"
    workflow_common.write(path, cfg)
    assert workflow_config.validate_config(workflow_common.read(path)) == cfg


def test_yaml_preserves_quoted_dates_and_versions(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text('date: "20261116"\nversion: "2.10.0"\n')
    assert workflow_config.read_config(path) == {
        "date": "20261116",
        "version": "2.10.0",
    }


@pytest.mark.parametrize("extension", ["json", ""])
def test_user_config_rejects_non_yaml(cfg, tmp_path, extension):
    path = tmp_path / ("config." + extension)
    workflow_common.write(path, cfg)
    with pytest.raises(ValueError, match="仅支持 .yaml / .yml"):
        workflow_config.config(path)


def minimal_config(cfg):
    return {
        "connection": cfg["connection"],
        "local": {"source_dir": cfg["local"]["source_dir"]},
        "remote": {"workspace": "/fixture/workspace"},
        "runtime": cfg["runtime"],
        "build": {k: cfg["build"][k] for k in ("llvm_build_dir", "lld_bin")},
    }


def test_minimal_config_resolves_paths_without_cwd(cfg, tmp_path, monkeypatch):
    raw = minimal_config(cfg)
    monkeypatch.chdir(tmp_path)
    c = workflow_config.validate_config(workflow_config.normalize_config(raw))
    assert c["local"]["source_dir"] == cfg["local"]["source_dir"]
    assert c["remote"]["source_dir"] == "/fixture/workspace/graph-autofusion-workflow"
    assert c["remote"]["state_dir"] == "/fixture/workspace/workflow-state"
    assert c["replay"]["results_dir"] == "/fixture/workspace/workflow-results"
    assert c["replay"]["devices"] == c["replay"]["workers"] == "auto"
    assert c["build"]["jobs"] == 8
    assert "debugger_module" not in c["runtime"]
    assert "state_dir" not in raw["local"]


def test_daily_python_and_cann_defaults(cfg):
    raw = minimal_config(cfg)
    raw["python_env"] = {
        "base_python": "/fixture/bin/python",
        "uv_bin": "/fixture/bin/uv",
        "torch_version": "2.10.0+cpu",
        "afx": {},
    }
    raw["cann"] = {"repository_url": "https://fixture.invalid/"}
    c = workflow_config.normalize_config(raw)
    assert c["python_env"]["state_dir"] == "/fixture/workspace/workflow-env-state"
    assert c["python_env"]["stable_requirements"][0] == "torch==2.10.0+cpu"
    assert c["python_env"]["afx"] == {"version": "latest"}
    assert c["python_env"]["torch_npu"]["date"] == "latest"
    assert c["python_env"]["torch_npu"]["version"] == "2.10.0"
    assert c["cann"]["auto_install"] and not c["cann"]["nnal"]


def test_config_rejects_debugger_option_and_unknown_defaults(cfg):
    raw = minimal_config(cfg)
    raw["runtime"] = dict(raw["runtime"], debugger_module="autofusex")
    with pytest.raises(ValueError, match="unknown configuration"):
        workflow_config.normalize_config(raw)
    raw["runtime"].pop("debugger_module")
    raw["replay"] = {"device": "auto"}
    with pytest.raises(ValueError, match="unknown configuration"):
        workflow_config.normalize_config(raw)


def test_config_explicit_directory_and_resource_overrides(cfg):
    raw = minimal_config(cfg)
    raw.update(local=cfg["local"], replay=dict(cfg["replay"]))
    raw["remote"]["source_dir"] = "/fixture/alternate/source"
    c = workflow_config.validate_config(workflow_config.normalize_config(raw))
    assert c["local"] == cfg["local"]
    assert c["replay"] == cfg["replay"]
    assert c["remote"]["source_dir"] == "/fixture/alternate/source"


def test_resolved_yaml_can_run_from_standalone_tools(cfg, tmp_path, monkeypatch):
    import yaml

    normalized = workflow_config.normalize_config(minimal_config(cfg))
    path = tmp_path / "resolved.yaml"
    path.write_text(yaml.safe_dump(normalized))
    monkeypatch.setattr(
        workflow_config,
        "__file__",
        str(tmp_path / "standalone/afxx/config.py"),
    )
    assert workflow_config.config(path) == normalized


def test_remote_snapshot_removed_only_after_successful_sync(cfg, tmp_path, monkeypatch):
    job = job_snapshot(cfg, tmp_path / "job")
    original = workflow_common.run

    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, a[0])

    monkeypatch.setattr(workflow_common, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        workflow_source.sync_source(cfg, job)
    assert (job / "snapshot/old.cpp").is_file()
    monkeypatch.setattr(workflow_common, "run", original)
    source, manifest = workflow_source.sync_source(cfg, job)
    assert not (job / "snapshot").exists()
    assert (job / "source.json").is_file()
    assert (
        workflow_common.read(job / "source.json")["snapshot_cleanup"]["state"]
        == "complete"
    )
    workflow_source.verify(source, workflow_common.read(job / "source.json"))
    assert workflow_common.read(job / "source.json")["sha256"] == manifest["sha256"]
    assert not (job / "snapshot-cleanup.json").exists()


def test_snapshot_cleanup_failure_does_not_fail_synced_source(
    cfg, tmp_path, monkeypatch
):
    job = job_snapshot(cfg, tmp_path / "job")

    def fail(*a, **kw):
        raise PermissionError("cannot delete snapshot")

    monkeypatch.setattr(shutil, "rmtree", fail)
    source, manifest = workflow_source.sync_source(cfg, job)
    workflow_source.verify(source, manifest)
    assert (job / "snapshot").is_dir()
    assert (
        workflow_common.read(job / "source.json")["snapshot_cleanup"]["state"]
        == "failed"
    )


def test_snapshot_cleanup_never_follows_root_symlink(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (job / "snapshot").symlink_to(outside, target_is_directory=True)
    assert workflow_source.cleanup_snapshot(job, "test")["state"] == "failed"
    assert (outside / "keep").read_text() == "keep"


def test_failed_sync_checksum_retains_remote_snapshot(cfg, tmp_path, monkeypatch):
    job = job_snapshot(cfg, tmp_path / "job")
    original = workflow_common.run

    def corrupt(argv, **kwargs):
        result = original(argv, **kwargs)
        (Path(cfg["remote"]["source_dir"]) / "old.cpp").write_text("corrupted")
        return result

    monkeypatch.setattr(workflow_common, "run", corrupt)
    with pytest.raises(ValueError, match="checksum"):
        workflow_source.sync_source(cfg, job)
    assert (job / "snapshot/old.cpp").read_text() == "old"


@pytest.mark.parametrize("mutation", ["deployment", "manifest", "missing_manifest"])
def test_deployment_reference_rejects_changed_evidence(
    cfg, tmp_path, monkeypatch, mutation
):
    job = job_snapshot(cfg, tmp_path / "job")
    manifest = workflow_common.read(job / "source.json")
    deployment = {
        "state": "success",
        "job": job.name,
        "environment": cfg["connection"]["environment"],
        "install_root": str(Path(cfg["runtime"]["install_root"]).resolve()),
        "source": {
            "path": str(job / "source.json"),
            "sha256": workflow_common.sha(job / "source.json"),
            "snapshot_sha256": manifest["sha256"],
            "base_commit": manifest["base_commit"],
        },
    }
    path = job / "result.json"
    workflow_common.write(path, deployment)
    monkeypatch.setattr(workflow_runtime, "registry", lambda c: tmp_path / "registry")
    workflow_common.write(
        tmp_path / "registry.json", workflow_runtime.deployment_reference(path)
    )
    assert workflow_runtime.current_deployment(cfg, path) == deployment
    if mutation == "deployment":
        workflow_common.update_record(path, package="changed")
    elif mutation == "manifest":
        workflow_common.update_record(job / "source.json", base_commit="changed")
    else:
        (job / "source.json").unlink()
    with pytest.raises(ValueError):
        workflow_runtime.current_deployment(cfg, path)


def test_workflow_root_derives_all_managed_paths(cfg):
    raw = minimal_config(cfg)
    raw.pop("remote")
    raw.pop("runtime")
    raw["workflow_root"] = "/fixture/workflow"
    raw["python_env"] = {}
    c = workflow_config.validate_config(workflow_config.normalize_config(raw))
    assert c["runtime"] == {
        "install_root": "/fixture/workflow/Ascend",
        "venv_dir": "/fixture/workflow/venv",
    }
    assert c["remote"]["source_dir"] == "/fixture/workflow/graph-autofusion"
    for path in [
        *c["remote"].values(),
        *c["runtime"].values(),
        c["replay"]["results_dir"],
        c["python_env"]["state_dir"],
    ]:
        assert Path(path).is_relative_to(raw["workflow_root"])
    assert workflow_config.normalize_config(c) == c
    assert "workflow_root" not in c


@pytest.mark.parametrize(
    "root",
    [
        None,
        "",
        "/",
        "/tmp",
        "relative/root",
        "/fixture/../other",
        "/fixture/with space",
        42,
    ],
)
def test_invalid_workflow_root(cfg, root):
    raw = minimal_config(cfg)
    raw.pop("remote")
    raw["workflow_root"] = root
    with pytest.raises(ValueError, match="invalid workflow_root"):
        workflow_config.normalize_config(raw)


def test_workflow_root_rejects_ambiguous_workspace(cfg):
    raw = minimal_config(cfg)
    raw["workflow_root"] = "/fixture/workflow"
    with pytest.raises(ValueError, match="not both"):
        workflow_config.normalize_config(raw)


def test_workflow_root_explicit_paths_preserve_existing_environment(cfg):
    raw = minimal_config(cfg)
    raw.pop("remote")
    raw["workflow_root"] = "/fixture/workflow"
    c = workflow_config.validate_config(workflow_config.normalize_config(raw))
    assert c["runtime"] == cfg["runtime"]
    assert c["remote"]["state_dir"] == "/fixture/workflow/workflow-state"


def test_daily_series_tracks_configured_torch(cfg):
    raw = minimal_config(cfg)
    raw["python_env"] = {"torch_version": "2.9.0+cpu"}
    raw["cann"] = {}
    config = workflow_config.normalize_config(raw)
    python_env = config["python_env"]
    daily = python_env["torch_npu"]
    assert daily["version"] == "2.9.0"
    assert "v2.9.0/" in daily["url_template"].format(
        version=daily["version"], date="20260101", python_tag="py311"
    )
    assert python_env["torch_index_url"].endswith("/cpu")
    assert config["cann"]["repository_url"].startswith("https://")
    raw["python_env"]["torch_version"] = "2.9.0"
    assert (
        workflow_config.normalize_config(raw)["python_env"]["torch_index_url"] is None
    )
    raw["python_env"]["torch_npu"] = {"version": "2.10.0"}
    with pytest.raises(ValueError, match="must match"):
        workflow_config.normalize_config(raw)


@pytest.mark.parametrize("extra", [None, {"FixtureChip": "FixtureOps"}])
def test_builtin_ops_mapping_survives_user_additions(cfg, extra):
    raw = minimal_config(cfg)
    raw["cann"] = {} if extra is None else {"ops_compatibility": extra}
    assert workflow_config.normalize_config(raw)["cann"]["ops_compatibility"] == {
        "Ascend910_9362": "A3",
        **(extra or {}),
    }


def test_cann_install_root_override(cfg):
    raw = minimal_config(cfg)
    raw.pop("remote")
    raw.pop("runtime")
    raw.update(workflow_root="/fixture/workflow", cann={"install_root": None})
    assert (
        workflow_config.normalize_config(raw)["runtime"]["install_root"]
        == "/fixture/workflow/Ascend"
    )
    raw["cann"]["install_root"] = "/fixture/shared/Ascend"
    resolved = workflow_config.validate_config(workflow_config.normalize_config(raw))
    assert resolved["runtime"]["install_root"] == "/fixture/shared/Ascend"
    assert workflow_config.normalize_config(resolved) == resolved
    raw["runtime"] = {"install_root": "/fixture/other/Ascend"}
    with pytest.raises(ValueError, match="conflicts"):
        workflow_config.normalize_config(raw)


@pytest.mark.parametrize("value", [None, "", "relative/source", "~/source"])
def test_local_source_must_be_explicit_absolute_path(cfg, value):
    raw = minimal_config(cfg)
    raw["local"]["source_dir"] = value
    with pytest.raises(ValueError, match="local.source_dir"):
        workflow_config.validate_config(workflow_config.normalize_config(raw))


def test_missing_local_source_does_not_fall_back_to_script_location(cfg):
    raw = minimal_config(cfg)
    del raw["local"]
    with pytest.raises(ValueError, match="local.source_dir"):
        workflow_config.validate_config(workflow_config.normalize_config(raw))


@pytest.mark.parametrize("configured,flag,expected", [(False, False, False), (True, False, True), (False, True, True)])
def test_replay_dump_override_is_frozen_without_editing_config(cfg, tmp_path, configured, flag, expected):
    import yaml
    cfg["replay"]["dump"] = configured
    path = tmp_path / "replay.yaml"
    original = yaml.safe_dump(cfg)
    path.write_text(original)
    result = subprocess.run(
        [sys.executable, "-m", "afxx.cli", "replay", "--config", str(path),
         "--request-id", "repair", "--seed", "F0SQR0HF7D3QEYXA8AE8QS7927Z", "--dry-run",
         *(["--dump"] if flag else [])], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)["data"]
    frozen = yaml.safe_load((Path(data["local"]) / "config.yaml").read_text())
    assert frozen["replay"]["dump"] is expected
    assert path.read_text() == original


@pytest.mark.parametrize('removed', ['wheel', 'sha256'])
def test_afx_rejects_removed_source_options(cfg, removed):
    raw = minimal_config(cfg)
    raw['python_env'] = {'afx': {removed: '/fixture/afx.whl' if removed == 'wheel' else 'a' * 64}}
    with pytest.raises(ValueError, match='unknown configuration'):
        workflow_config.normalize_config(raw)
