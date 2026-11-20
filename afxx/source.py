#!/usr/bin/env python3
"""Source snapshots, integrity checks, synchronization and cleanup."""

import os
from pathlib import Path
import shutil
import stat
import sys
from afxx import common as workflow_common


def entry(path):
    mode = path.lstat().st_mode
    workflow_common.require(
        stat.S_ISREG(mode) or stat.S_ISLNK(mode), "not a file: " + str(path)
    )
    return (
        {"mode": stat.S_IMODE(mode), "link": os.readlink(path)}
        if path.is_symlink()
        else {"mode": stat.S_IMODE(mode), "sha256": workflow_common.sha(path)}
    )


def safe_path(root, name):
    p = root / workflow_common.relative(name)
    workflow_common.require(
        not workflow_common.protected(name), "protected file: " + name
    )
    workflow_common.require(
        workflow_common.inside(p.parent.resolve(), root.resolve()),
        "symlink parent escapes source: " + name,
    )
    parent = p.parent
    while parent != root:
        workflow_common.require(
            not parent.is_symlink(), "symlink parent in source: " + name
        )
        parent = parent.parent
    return p


def selected_names(c, source):
    names = set(
        workflow_common.capture(
            [
                "git",
                "-C",
                source,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ]
        ).split("\0")
    ) - {""}
    for name in c["sync"]["include_ignored_paths"]:
        path = safe_path(source, name)
        workflow_common.require(
            path.exists() or path.is_symlink(), "missing included path: " + name
        )
        names.update(
            str(p.relative_to(source))
            for p in path.rglob("*")
            if p.is_file() or p.is_symlink()
        ) if path.is_dir() else names.add(name)
    names = {
        n
        for n in names
        if not workflow_common.protected(n)
        and not any(
            workflow_common.inside(Path(n), Path(e)) for e in c["sync"]["exclude_paths"]
        )
    }
    return {n for n in names if (source / n).exists() or (source / n).is_symlink()}


def snapshot(c, destination):
    source = Path(c["local"]["source_dir"]).resolve()
    names = selected_names(c, source)
    manifest = {}
    for name in sorted(names):
        path = safe_path(source, name)
        if not path.exists() and not path.is_symlink():
            continue  # A tracked deletion is deliberately absent from the snapshot.
        if path.is_symlink():
            workflow_common.require(
                not Path(os.readlink(path)).is_absolute()
                and workflow_common.inside(path.resolve(), source),
                "external source symlink: " + name,
            )
            workflow_common.require(
                not workflow_common.protected(str(path.resolve().relative_to(source))),
                "symlink targets protected file: " + name,
            )
        manifest[name] = entry(path)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target, follow_symlinks=False)
        workflow_common.require(
            entry(target) == manifest[name], "source changed while copying: " + name
        )
    for name, expected in manifest.items():
        workflow_common.require(
            entry(safe_path(source, name)) == expected,
            "source changed during snapshot: " + name,
        )
    workflow_common.require(
        selected_names(c, source) == set(manifest),
        "source file set changed during snapshot",
    )
    workflow_common.require("build.sh" in manifest, "snapshot must contain build.sh")
    return {
        "files": manifest,
        "sha256": workflow_common.digest(manifest),
        "base_commit": workflow_common.capture(
            ["git", "-C", source, "rev-parse", "HEAD"]
        ).strip(),
    }


def inventory(root):
    names = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in list(directories):
            path = base / name
            relative_name = str(path.relative_to(root))
            if workflow_common.protected(relative_name):
                directories.remove(name)
            elif path.is_symlink():
                names.add(relative_name)
                directories.remove(name)
        for name in files:
            relative_name = str((base / name).relative_to(root))
            if not workflow_common.protected(relative_name):
                names.add(relative_name)
    return names


def verify(root, manifest):
    for name, expected in manifest["files"].items():
        workflow_common.require(
            entry(safe_path(root, name)) == expected,
            "source checksum mismatch: " + name,
        )
    workflow_common.require(
        workflow_common.digest(manifest["files"]) == manifest["sha256"],
        "manifest hash mismatch",
    )


def cleanup_snapshot(job, reason):
    """Delete only the task-owned snapshot; record cleanup in its existing manifest."""
    path = job / "snapshot"
    report = {"reason": reason, "state": "complete"}
    try:
        workflow_common.require(
            not job.is_symlink() and not path.is_symlink(),
            "refusing symlink snapshot cleanup",
        )
        if path.exists():
            workflow_common.require(path.is_dir(), "snapshot is not a directory")
            shutil.rmtree(path)
    except (OSError, ValueError) as error:
        report.update(state="failed", error=str(error))
        print("Snapshot cleanup failed: " + str(error), file=sys.stderr)
    try:
        manifest = workflow_common.read(job / "source.json")
        manifest["snapshot_cleanup"] = report
        workflow_common.write(job / "source.json", manifest)
    except (OSError, ValueError) as error:
        report.update(state="failed", error=str(error))
        print("Cannot save snapshot cleanup report: " + str(error), file=sys.stderr)
    return report


def sync_source(c, job):
    source = Path(c["remote"]["source_dir"])
    workflow_common.require(
        not source.is_symlink(), "source directory may not be a symlink"
    )
    source.mkdir(parents=True, exist_ok=True)
    marker = source / ".af-workflow"
    workflow_common.require(
        marker.exists() or not any(source.iterdir()),
        "refusing unmanaged remote source directory",
    )
    old = workflow_common.read(marker) if marker.exists() else {"files": {}}
    if old.get("complete", True):
        workflow_common.require(
            inventory(source) == set(old["files"]), "unmanaged files in remote source"
        )
        if old["files"]:
            verify(source, old)
    new = workflow_common.read(job / "source.json")
    verify(job / "snapshot", new)
    # Persist ownership BEFORE changing files so interrupted transfers are recoverable.
    pending = dict(old["files"])
    pending.update(new["files"])
    workflow_common.write(
        marker,
        {
            "files": pending,
            "sha256": workflow_common.digest(pending),
            "complete": False,
        },
    )
    # Delete only files owned by a previous complete or interrupted synchronization.
    for name in sorted(
        set(old["files"]) - set(new["files"]),
        key=lambda n: len(Path(n).parts),
        reverse=True,
    ):
        path = safe_path(source, name)
        workflow_common.require(
            not path.is_dir() or path.is_symlink(),
            "managed file became directory: " + name,
        )
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != source:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    for name in new["files"]:
        safe_path(source, name)
    workflow_common.run(
        ["rsync", "-a", "--checksum", str(job / "snapshot") + "/", str(source) + "/"]
    )
    verify(source, new)
    workflow_common.write(marker, dict(new, complete=True))
    cleanup_snapshot(job, "source_sync_verified")
    return source, new
