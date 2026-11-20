"""Layer boundaries and remote execution without the source repository on sys.path."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

from af_forge import bundle as workflow_bundle
from af_forge import jobs as workflow_jobs


def test_module_dependencies_are_acyclic_and_do_not_import_cli():
    root = Path(workflow_bundle.__file__).parent.parent
    names = {
        ".".join(Path(name).with_suffix("").parts)
        for name in workflow_bundle.FILES
        if name.endswith(".py")
    }
    edges = {}
    for name in names:
        tree = ast.parse((root / (name.replace(".", "/") + ".py")).read_text())
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    base = importlib.util.resolve_name(
                        "." * node.level + base, name.rsplit(".", 1)[0]
                    )
                imports.add(base)
                imports.update(base + "." + item.name for item in node.names)
        edges[name] = imports & names
        assert "af_forge.cli" not in edges[name]

    def visit(name, ancestors):
        assert name not in ancestors, f"cyclic dependency: {ancestors} -> {name}"
        for dependency in edges[name]:
            visit(dependency, ancestors + [name])

    for name in names:
        visit(name, [])


def test_submitted_bundle_imports_without_repository(cfg, tmp_path):
    # All job commands ship the same bundle; exercise submission and isolated imports once.
    command = "deploy"
    result = workflow_jobs.submit(cfg, {"command": command}, "bundle-" + command, True)
    directory = Path(result["local"])
    modules = [
        ".".join(Path(name).with_suffix("").parts)
        for name in workflow_bundle.FILES
        if name.endswith(".py")
    ]
    # Python 3.9 test hosts need tomli; NPU Python 3.11 provides tomllib.
    dependency_path = ""
    if sys.version_info < (3, 11):
        dependency_path = str(
            Path(importlib.util.find_spec("tomli").origin).parent.parent
        )
    script = """import importlib, json, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
if sys.argv[3]:
    sys.path.append(sys.argv[3])
for name in json.loads(sys.argv[2]):
    module = importlib.import_module(name)
    assert root in Path(module.__file__).resolve().parents, module.__file__
assert json.loads((root / 'af_forge/environment/package_sources.json').read_text())
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(directory),
            json.dumps(modules),
            dependency_path,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def test_copied_runtime_dependency_probe_is_executable(tmp_path):
    workflow_bundle.copy_to(tmp_path)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "af_forge.environment.runtime",
            "_python_check",
            "missing_workflow_fixture",
            "replay",
            sys.prefix,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"] is False
    assert any(
        row["name"] == "missing_workflow_fixture" and not row["passed"]
        for row in report["checks"]
    )
