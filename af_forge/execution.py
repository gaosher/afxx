#!/usr/bin/env python3
"""Deployment and direct replay stages."""

import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from af_forge.afx import replay as replay_adapter
from af_forge import common as workflow_common
from af_forge.environment import runtime as workflow_runtime
from af_forge import source as workflow_source


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
        # Remove the obsolete Forge stamp when reusing a pre-existing build tree.
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
        workflow_runtime.check_idle(
            c["runtime"]["install_root"],
            library_names=workflow_runtime.packaged_libraries(source / "build"),
        )
        workflow_common.phase(job, "install")
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
            workflow_common.write(job / "deployment.json", record)
            workflow_common.write(
                current, workflow_runtime.deployment_reference(job / "deployment.json")
            )
        except BaseException:
            workflow_common.write(current, {"state": "invalid", "job": job.name})
            raise


def record_environment(c, job):
    from af_forge.environment import snapshot

    workflow_common.phase(job, "environment_snapshot")
    snapshot.capture(c, job)


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
        afx=dict(afx["source"], sha256=afx["sha256"]),
    )
    if p["afx"].get("wheel") and Path(p["afx"]["wheel"]).is_absolute():
        p["afx"]["wheel"] = afx["path"]
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
    results = Path(c["replay"]["results_dir"]) / job.name
    attempts = []
    try:
        with (
            workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
            workflow_common.lock(
                workflow_runtime.registry(c).with_suffix(".lock"), shared=True
            ),
        ):
            deployment = workflow_runtime.current_deployment(c, request["deployment"])
            record_environment(c, job)
            workflow_common.update_record(
                job / "report.json",
                state="running",
                passed=False,
                deployment=deployment["job"],
            )
            env = workflow_runtime.environment(c)
            workflow_runtime.venv_preflight(c, env, job, "replay")
            runtime = workflow_runtime.probe(c, env, job)
            expected_runtime = deployment["runtime"]
            workflow_common.require(
                {k: v for k, v in runtime.items() if k != "debugger"}
                == {k: v for k, v in expected_runtime.items() if k != "debugger"},
                "installed AF runtime changed",
            )
            if runtime.get("debugger") != expected_runtime.get("debugger"):
                print(
                    f"WARNING: AFX changed since deployment: "
                    f"expected={expected_runtime.get('debugger')!r}, "
                    f"actual={runtime.get('debugger')!r}; continuing replay",
                    file=sys.stderr,
                    flush=True,
                )
            workflow_common.update_record(job / "report.json", runtime=runtime)
            results = Path(c["replay"]["results_dir"]) / job.name
            results.mkdir(parents=True, mode=0o700)
            replay_env = replay_adapter.replay_environment(results, c["replay"]["dump"])
            env = {**env, "AUTOFUSEX_HOME": replay_env["AUTOFUSEX_HOME"]}
            workflow_common.update_record(job / "report.json", replay_environment=replay_env)
            workflow_common.phase(job, "replay_prepare", results_dir=str(results))
            options = dict(c["replay"])
            if options["devices"] == "auto":
                count = replay_adapter.visible_device_count(
                    workflow_common.capture(
                        replay_adapter.device_command(workflow_runtime.python(c)),
                        env=env,
                        cwd=job,
                        timeout=60,
                    )
                )
                options["devices"] = ["npu:" + str(index) for index in range(count)]
            if options["workers"] == "auto":
                options["workers"] = len(options["devices"])
            if not request["batch"]:
                options["devices"] = options["devices"][:1]
                options["workers"] = 1
            workflow_common.update_record(
                job / "report.json", resources=options, results_dir=str(results)
            )
            normalized = replay_adapter.normalized_seeds(
                workflow_common.capture(
                    replay_adapter.seed_command(
                        workflow_runtime.python(c), request["seeds"]
                    ),
                    env=env,
                    cwd=job,
                )
            )
            upload = (
                request["upload"]
                if request["upload"] is not None
                else c["upload"]["enabled"]
            )
            if upload:
                shutil.copyfile(c["upload"]["sync_config_path"], results / "sync.json")
                (results / "sync.json").chmod(0o600)
            command = replay_adapter.replay_command(
                workflow_runtime.python(c),
                options,
                normalized,
                results,
                request["batch"],
            )
            workflow_common.phase(job, "replay", results_dir=str(results))
            # --save persists evidence locally. Without sync.json no upload is configured.
            completed = workflow_common.run(command, env={**env, **replay_env}, cwd=job, check=False)
            workflow_common.update_record(
                job / "report.json", exit_code=completed.returncode
            )
            attempts = replay_adapter.read_results(
                workflow_runtime.python(c),
                results / "results.sqlite",
                lambda argv: workflow_common.capture(argv, env=env, cwd=job),
            )
            report = replay_adapter.summarize(attempts, normalized)
            report.update(
                resources=options,
                deployment=deployment["job"],
                results_dir=str(results),
                exit_code=completed.returncode,
                upload="pending" if upload else "disabled",
            )
            report = workflow_common.update_record(job / "report.json", **report)
            if upload:
                sync = replay_adapter.sync_command(workflow_runtime.python(c), results)
                report["sync_start_exit_code"] = workflow_common.run(
                    sync, env=env, cwd=job, check=False
                ).returncode
                report["upload"] = (
                    "pending" if report["sync_start_exit_code"] == 0 else "start_failed"
                )
            workflow_common.update_record(job / "report.json", **report)
            workflow_common.require(
                completed.returncode == 0 and report["passed"],
                "retest failed; see report.json",
            )
            workflow_common.require(
                report["upload"] != "start_failed",
                "tests passed but sync failed to start",
            )
            workflow_common.update_record(job / "report.json", state="success")
    except BaseException as error:
        workflow_common.update_record(
            job / "report.json", state="failed", error=str(error)
        )
        raise
    finally:
        # Collection failures must not hide the original replay failure.
        try:
            evidence = replay_adapter.collect_evidence(job, results, c["replay"]["dump"])
            replay_adapter.associate_evidence(evidence, attempts)
        except OSError as error:
            evidence = {"errors": [{"error": str(error)}], "files": []}
        workflow_common.update_record(job / "report.json", evidence=evidence)
