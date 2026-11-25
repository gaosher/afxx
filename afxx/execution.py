#!/usr/bin/env python3
"""Deployment and direct replay stages."""

import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from afxx.afx import replay as replay_adapter
from afxx import common as workflow_common
from afxx import config as workflow_config
from afxx.environment import runtime as workflow_runtime
from afxx import source as workflow_source


def deploy(c, job):
    source_path = Path(c["remote"]["source_dir"]).resolve()
    source_lock = workflow_runtime.registry(c).parent / (
        "source-" + workflow_common.digest(str(source_path)) + ".lock"
    )
    with (
        workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
        workflow_common.lock(source_lock),
        workflow_common.lock(workflow_runtime.registry(c).with_suffix(".lock")),
    ):
        record_environment(c, job)
        env = workflow_runtime.environment(c)
        workflow_runtime.venv_preflight(c, env, job, "deploy")
        workflow_common.phase(job, "sync")
        source, manifest = workflow_source.sync_source(c, job)
        workflow_common.phase(job, "build")
        build_dir = source / "build"
        workflow_common.require(not build_dir.is_symlink(), "build directory may not be a symlink")
        build_dir.mkdir(exist_ok=True)
        # Remove the obsolete AFXX stamp when reusing a pre-existing build tree.
        (build_dir / ".workflow-environment.json").unlink(missing_ok=True)
        output = job / "packages"
        output.mkdir()
        workflow_common.run(
            [
                "bash",
                "build.sh",
                "--pkg",
                "--enable-mlir",
                "-j",
                str(c["build"]["jobs"]),
                "--output_path=" + str(output),
            ],
            cwd=source,
            env=env,
        )
        packages = list(output.glob("cann-graph-autofusion_*.run"))
        workflow_common.require(
            len(packages) == 1, "expected exactly one fresh AF run package"
        )
        package = packages[0]
        workflow_common.run(["bash", package, "--check"], env=env, cwd=job)
        package_hash = workflow_common.sha(package)
        from afxx.deploy_client import coordinate
        coordination = None
        try:
            with coordinate(c, job) as coordination:
                workflow_runtime.check_idle(
                    c["runtime"]["install_root"],
                    library_names=workflow_runtime.packaged_libraries(source / "build"),
                )
                workflow_common.phase(job, "install")
                if coordination is not None:
                    coordination.install_started = True
                current = workflow_runtime.registry(c).with_suffix(".json")
                workflow_common.write(current, {"state": "installing", "job": job.name})
                try:
                    workflow_common.run(
                        [
                            "bash",
                            package,
                            "--upgrade",
                            "--install-path=" + c["runtime"]["install_root"],
                            "--pylocal",
                            "--quiet",
                        ],
                        env=env,
                        cwd=job,
                    )
                    workflow_common.phase(job, "verify_install")
                    evidence = workflow_runtime.probe(c, workflow_runtime.environment(c), job)
                    for key in ("pyautofuse", "adapter", "codegen"):
                        candidates = [
                            p
                            for p in (source / "build").rglob(Path(evidence[key]["path"]).name)
                            if p.is_file()
                        ]
                        workflow_common.require(
                            any(
                                workflow_common.sha(p) == evidence[key]["sha256"]
                                for p in candidates
                            ),
                            "installed file does not match build: " + key,
                        )
                    record = {
                        "state": "success",
                        "job": job.name,
                        "environment": c["connection"]["environment"],
                        "install_root": str(Path(c["runtime"]["install_root"]).resolve()),
                        "source": {
                            "path": str((job / "source.json").resolve()),
                            "sha256": workflow_common.sha(job / "source.json"),
                            "snapshot_sha256": manifest["sha256"],
                            "base_commit": manifest["base_commit"],
                        },
                        "package": str(package),
                        "package_sha256": package_hash,
                        "runtime": evidence,
                        "config_sha256": workflow_common.digest(c),
                        "installed_at": datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat(),
                    }
                    if coordination is not None:
                        record["client_coordination"] = coordination.record
                    workflow_common.write(job / "result.json", record)
                    workflow_common.write(
                        current, workflow_runtime.deployment_reference(job / "result.json")
                    )
                    if coordination is not None:
                        coordination.installed = True
                except BaseException:
                    workflow_common.write(current, {"state": "invalid", "job": job.name})
                    raise
        finally:
            if coordination is not None and coordination.installed:
                # Coordination adds its receipt after installation; refresh the report hash.
                workflow_common.write(workflow_runtime.registry(c).with_suffix(".json"),
                    workflow_runtime.deployment_reference(job / "result.json"))


def record_environment(c, job):
    from afxx.environment import snapshot

    workflow_common.phase(job, "environment_snapshot")
    return snapshot.capture(c, job)


def save_frozen_config(c, job, manifest):
    frozen = frozen_config(c, manifest)
    # The system Python that runs the worker may lack PyYAML; the verified venv has it.
    try:
        import yaml
    except ImportError:
        script = "import json,sys,yaml; yaml.safe_dump(json.load(sys.stdin),sys.stdout,sort_keys=False)"
        body = subprocess.run(
            [workflow_runtime.python(c), "-I", "-c", script],
            input=json.dumps(frozen),
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    else:
        body = yaml.safe_dump(frozen, sort_keys=False)
    path = job / "config.yaml"
    temporary = path.with_suffix(".yaml.tmp")
    temporary.write_text(body)
    temporary.replace(path)


def frozen_config(c, manifest):
    wheels = manifest["artifacts"]
    daily = wheels["torch_npu"]
    afx = wheels["afx"]
    p = dict(
        c["python_env"],
        torch_npu=daily["source"],
        afx={"version": afx["metadata"]["version"]},
    )
    locked = Path(manifest["lock"]["path"]).read_text()
    workflow_common.require(
        hashlib.sha256(locked.encode()).hexdigest() == manifest["lock"]["sha256"],
        "Python lock hash mismatch",
    )
    pins = {
        "python": manifest["python"],
        "packages": manifest["packages"],
        "pylock": locked,
        "pylock_sha256": manifest["lock"]["sha256"],
        "cann": manifest["cann"],
        "wheels": {name: item["sha256"] for name, item in wheels.items()},
    }
    if p.get("python_dir") and p.get("base_python") and workflow_common.inside(Path(p["base_python"]).resolve(), Path(p["python_dir"]).resolve()):
        p["base_python"] = None
    if p.get("uv_bin") == str(Path(c["runtime"]["venv_dir"]) / "bin/uv"):
        p["uv_bin"] = None
    return dict(c, python_env=p, cann=manifest["cann_config"], environment_lock=pins)


def replay(c, job, request):
    from afxx.afx import fuzz_replay
    fuzz_replay.validate_options(c, request)
    results = Path(c["replay"]["results_dir"]) / job.name
    results.mkdir(parents=True, exist_ok=True)
    try:
        with (
            workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
            workflow_common.lock(workflow_runtime.registry(c).with_suffix(".lock"), shared=True),
        ):
            snapshot = record_environment(c, job)
            workflow_common.update_record(job / "result.json", state="running", passed=False,
                request_id=request.get("request_id"), run=request.get("run"),
                submitter_runtime=snapshot.get("af"), execution_mode="fuzz_replay")
            completed = workflow_common.run(
                [workflow_runtime.python(c), "-m", "afxx.afx.fuzz_replay", str(job)],
                env=workflow_runtime.environment(c), cwd=job, check=False,
            )
            report = workflow_common.read(job / "result.json")
            workflow_common.require_code(completed.returncode == 0 and report.get("passed"),
                report.get("error_code", "AFX_REPLAY_FAILED"), report.get("error", "fuzz replay did not complete"))
            workflow_common.update_record(job / "result.json", state="success")
    except BaseException as error:
        workflow_common.update_record(job / "result.json", state="failed", error=str(error))
        raise
    finally:
        try:
            evidence = replay_adapter.collect_evidence(job, results, c["replay"]["dump"])
            replay_adapter.associate_evidence(evidence, workflow_common.read(job / "result.json").get("attempts", []))
        except OSError as error:
            evidence = {"errors": [{"error": str(error)}], "files": []}
        workflow_common.update_record(job / "result.json", evidence=evidence)


def fuzz(c, job, request):
    """Keep the official foreground supervisor inside an asynchronous workflow job."""
    from afxx.afx import fuzz as adapter

    directory = adapter.home(c)
    workflow_common.phase(job, "fuzz")
    with (
        adapter.running(directory, job) as run_fd,
        workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
        workflow_common.lock(workflow_runtime.registry(c).with_suffix(".lock"), shared=True),
    ):
        record_environment(c, job)
        env = dict(workflow_runtime.environment(c), AUTOFUSEX_HOME=str(directory))
        workflow_runtime.venv_preflight(c, env, job, "replay")
        command = adapter.command(c, directory, request["fuzz"])
        workflow_common.write(job / "result.json", {
            "command": command, "results_dir": str(directory),
            "database": str(directory / "results/results.sqlite"),
        })
        workflow_common.phase(job, "fuzz")
        completed = workflow_common.run(command, env=env, cwd=directory, check=False, pass_fds=(run_fd,))
        workflow_common.update_record(job / "result.json", exit_code=completed.returncode)
        workflow_common.require_code(completed.returncode == 0, "FUZZ_FAILED",
                                     f"AFX fuzz exited with code {completed.returncode}; see job.log")
