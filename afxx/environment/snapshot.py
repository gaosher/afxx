"""Capture live job environment without comparing it with installation history."""

import datetime
import json
import os
import subprocess
from pathlib import Path

from afxx import common
from afxx.environment import runtime

PACKAGE_PROBE = r"""
import importlib.metadata as md, json, platform, sys
from urllib.parse import urlsplit, urlunsplit
packages = {}
for dist in md.distributions():
    name = dist.metadata.get('Name')
    if not name:
        continue
    item = {'version': dist.version, 'location': str(dist.locate_file(''))}
    origin = json.loads(dist.read_text('direct_url.json') or '{}')
    if origin:
        url = urlsplit(origin.get('url', ''))
        host = url.netloc.rsplit('@', 1)[-1]
        origin['url'] = urlunsplit((url.scheme, host, url.path, '', ''))
        item['source'] = origin
    packages[name] = item
print(json.dumps({'version': platform.python_version(), 'executable': sys.executable,
                  'prefix': sys.prefix, 'packages': packages}))
"""


def capture(c, job=None):
    report = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # noqa: UP017 - remote worker supports Python 3.9
        "connection": c["connection"],
        "runtime": c["runtime"],
        "errors": [],
    }
    root = Path(c["runtime"]["install_root"]) / "cann"
    report["cann"] = {"path": str(root.resolve()), "components": {}}
    for component, relative in [
        ("toolkit", "compiler/version.info"),
        ("ops", "opp/version.info"),
    ]:
        try:
            report["cann"]["components"][component] = (root / relative).read_text()
        except OSError as error:
            report["errors"].append(
                {"component": "cann." + component, "error": str(error)}
            )
    try:
        env = runtime.environment(c)
        report["python"] = json.loads(
            common.capture(
                [runtime.python(c), "-c", PACKAGE_PROBE], env=env, timeout=120
            ).splitlines()[-1]
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report["errors"].append({"component": "python", "error": str(error)})
    else:
        try:
            report["af"] = runtime.probe(c, env, job)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            report["errors"].append({"component": "af", "error": str(error)})
    if job is not None:
        common.write(Path(job) / "environment.json", report)
    return report


def inspect_environment(c):
    """Read live environment and dependencies without creating a workflow task."""
    report = capture(c)
    try:
        env = runtime.environment(c)
        code = ("import json; from afxx.environment.runtime import python_dependency_report; "
                "print(json.dumps(python_dependency_report('autofusex', 'full', "
                + repr(c['runtime']['venv_dir']) + ")))")
        # The temporary adapter directory is code only, removed by the caller.
        env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
        dependencies = json.loads(common.capture([runtime.python(c), '-c', code], env=env, timeout=1800))
        report['dependencies'] = dependencies
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report['state'] = 'failed'
        raise common.WorkflowError('ENV_CHECK_FAILED', str(error), report) from error
    report['state'] = 'success' if dependencies['passed'] else 'failed'
    if not dependencies['passed']:
        failed = [item['name'] for item in dependencies['checks'] if not item['passed']]
        raise common.WorkflowError('ENV_CHECK_FAILED', 'Python dependency check failed: ' + ', '.join(failed), report)
    return report
