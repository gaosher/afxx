#!/usr/bin/env python3
"""Files required to run a workflow independently of the local repository."""

from pathlib import Path
import shutil

FILES = (
    "afxx/cli.py",
    "afxx/credentials.py",
    "afxx/config_save.py",
    "afxx/__init__.py",
    "afxx/environment/__init__.py",
    "afxx/afx/__init__.py",
    "afxx/environment/python.py",
    "afxx/environment/bootstrap.py",
    "afxx/environment/cann.py",
    "afxx/common.py",
    "afxx/config.py",
    "afxx/source.py",
    "afxx/environment/runtime.py",
    "afxx/execution.py",
    "afxx/deploy_client.py",
    "afxx/jobs.py",
    "afxx/replay_runs.py",
    "afxx/run_groups.py",
    "afxx/deploy_runs.py",
    "afxx/job_view.py",
    "afxx/evidence_bundle.py",
    "afxx/bundle.py",
    "afxx/environment/snapshot.py",
    "afxx/afx/replay.py",
    "afxx/afx/fuzz_replay.py",
    "afxx/afx/fuzz.py",
    "afxx/afx/platform.py",
    "afxx/afx/launcher.py",
    "afxx/afx/managed_client.py",
    "afxx/environment/package_sources.json",
    "afxx/resources/config.example.yaml",
)


def copy_to(destination):
    destination = Path(destination)
    root = Path(__file__).resolve().parent.parent
    for name in FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
