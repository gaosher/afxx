"""Bootstrap a missing CANN from a configured release catalog; upgrade only explicitly selected components."""

from html.parser import HTMLParser
from afxx import common as workflow_common

import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request


def https(value):
    parsed = urllib.parse.urlsplit(value)
    workflow_common.require(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "invalid CANN repository URL",
    )
    return value


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.values.append(dict(attrs).get("href", ""))


def listing(url):
    with urllib.request.urlopen(https(url), timeout=60) as response:
        https(response.url)
        page = response.read().decode("utf-8")
    parser = Links()
    parser.feed(page)
    return parser.values


def fields(text):
    return {
        key.strip(): value.strip()
        for line in text.splitlines()
        if ":" in line
        for key, value in [line.split(":", 1)]
    }


def detect_ops(cfg):
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        [*cfg["driver_library_dirs"], env.get("LD_LIBRARY_PATH", "")]
    )
    mapping = workflow_common.capture(["npu-smi", "info", "-m"], env=env)
    chips = set()
    for line in mapping.splitlines():
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s+(.+)", line)
        if match:
            chips.add((match[1], match[2]))
    workflow_common.require(chips, "npu-smi did not report accessible NPU chips")
    evidence, families = [], set()
    for board, chip in sorted(chips):
        info = fields(
            workflow_common.capture(
                ["npu-smi", "info", "-t", "board", "-i", board, "-c", chip], env=env
            )
        )
        name = info.get("Chip Name", "").replace(" ", "")
        number = info.get("NPU Name", "").replace(" ", "")
        identity = name + "_" + number if number and number != "NA" else name
        family = cfg["ops_compatibility"].get(identity)
        workflow_common.require(
            family,
            f"Unknown OPS mapping for {identity!r}. After verifying the OPS family, "
            f'add it under cann.ops_compatibility, e.g. {{{json.dumps(identity)}: "<OPS family>"}}',
        )
        evidence.append(
            {"board": board, "chip": chip, "identity": identity, "ops": family}
        )
        families.add(family)
    workflow_common.require(
        len(families) == 1, "mixed OPS families require separate environments"
    )
    return families.pop(), evidence


def select_ops(cfg):
    ops = cfg["ops"]
    if ops in (None, "", "auto"):
        return detect_ops(cfg)
    if ops == "none":
        return None, []
    workflow_common.require(
        isinstance(ops, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", ops),
        "cann.ops must be null, auto, none, or a quoted OPS package family",
    )
    return ops, []


def select_release(cfg, family, architecture, frozen_release=None):
    root = https(cfg["repository_url"]).rstrip("/") + "/"
    releases = sorted(
        {n.rstrip("/") for n in listing(root) if re.fullmatch(r"\d+/", n)},
        key=int,
        reverse=True,
    )
    date = cfg.get("date", "latest")
    if date != "latest":
        releases = [release for release in releases if release.startswith(date)]
    if frozen_release is not None:
        releases = [r for r in releases if r == frozen_release]
    skipped = []
    for release in releases:
        base = urllib.parse.urljoin(root, release + "/")
        entries = set(listing(base))
        pattern = (
            r"Ascend-cann-toolkit_([A-Za-z0-9.~+_-]+)_linux-"
            + re.escape(architecture)
            + r"\.run"
        )
        candidates = [(name, re.fullmatch(pattern, name)) for name in entries]
        candidates = [(name, match) for name, match in candidates if match]
        workflow_common.require(
            len(candidates) <= 1, "ambiguous Toolkit packages in release " + release
        )
        if not candidates:
            skipped.append(release)
            continue
        toolkit, match = candidates[0]
        version = match.group(1)
        packages = [("toolkit", toolkit)]
        if family:
            packages.append(
                ("ops", f"Ascend-cann-{family}-ops_{version}_linux-{architecture}.run")
            )
        if cfg["nnal"]:
            packages.append(
                ("nnal", f"Ascend-cann-nnal_{version}_linux-{architecture}.run")
            )
        if all(name in entries for _, name in packages):
            return {
                "release": release,
                "version": version,
                "base_url": base,
                "packages": packages,
                "skipped_incomplete": skipped,
            }
        skipped.append(release)
    raise ValueError(
        f"no complete CANN release matches date={date}, architecture={architecture} and requested components"
    )


def ready(root):
    return (root / "cann/set_env.sh").is_file() and (
        root / "cann/compiler/version.info"
    ).is_file()


def ensure(c, job):
    root = Path(c["runtime"]["install_root"])
    state = root / ".af-cann-bootstrap.json"
    workflow_common.require(
        not state.exists() or workflow_common.read(state).get("state") == "ready",
        "previous CANN bootstrap failed; inspect installation before retrying",
    )
    if ready(root):
        info = dict(
            line.split("=", 1)
            for line in (root / "cann/compiler/version.info").read_text().splitlines()
            if "=" in line
        )
        workflow_common.update_record(
            job / "result.json",
            cann={
                "state": "existing",
                "install_root": str(root),
                "installed_version": info.get("Version"),
                "installed_release": info.get("timestamp", "").replace("_", ""),
            },
        )
        return
    workflow_common.require(
        (not root.exists() and not root.is_symlink())
        or (root.is_dir() and not any(root.iterdir())),
        "partial CANN installation or nonempty install root: " + str(root),
    )
    cfg = c.get("cann")
    workflow_common.require(
        cfg and cfg.get("auto_install") is True,
        "CANN missing; configure cann.auto_install to bootstrap it",
    )
    workflow_common.require(
        set(cfg) - {"date"}
        == {
            "auto_install",
            "repository_url",
            "ops",
            "nnal",
            "ops_compatibility",
            "driver_library_dirs",
        },
        "invalid cann config fields",
    )
    workflow_common.require(type(cfg["nnal"]) is bool, "invalid CANN component options")
    workflow_common.require(
        isinstance(cfg["ops_compatibility"], dict)
        and all(
            isinstance(k, str)
            and isinstance(v, str)
            and re.fullmatch(r"[A-Za-z0-9_-]+", v)
            for k, v in cfg["ops_compatibility"].items()
        ),
        "invalid OPS compatibility table",
    )
    workflow_common.require(
        isinstance(cfg["driver_library_dirs"], list)
        and all(
            isinstance(v, str) and Path(v).is_absolute()
            for v in cfg["driver_library_dirs"]
        ),
        "invalid driver library directories",
    )
    workflow_common.phase(job, "cann_preflight")
    family, hardware = select_ops(cfg)
    pins = c.get("environment_lock", {}).get("cann")
    if pins and pins.get("components"):
        release = select_release(dict(cfg, nnal=False), None, platform.machine(), pins["release"])
        release["packages"] = [(item["component"], item["name"]) for item in pins["components"]]
    else:
        release = (
            select_release(cfg, family, platform.machine(), pins["release"])
            if pins else select_release(cfg, family, platform.machine()))
    report = download_release(c, job, release, hardware, pins)
    install_release(c, job, report)


def prepare_update(c, job, date, with_ops, previous):
    root = Path(c["runtime"]["install_root"])
    workflow_common.require(ready(root), "CANN update requires an existing complete Toolkit")
    cfg = dict(c["cann"], date=date, nnal=False)
    family, hardware = select_ops(cfg) if with_ops else (None, [])
    workflow_common.require(not with_ops or family, "--ops requires an OPS family; cann.ops cannot be none")
    # Pick Toolkit first: never silently fall back to an older batch to find OPS.
    release = select_release(cfg, None, platform.machine())
    if family:
        name = f"Ascend-cann-{family}-ops_{release['version']}_linux-{platform.machine()}.run"
        workflow_common.require(name in listing(release["base_url"]), "matching OPS package missing from selected CANN release")
        release["packages"].append(("ops", name))
    workflow_common.require(not previous.get("release") or int(release["release"]) >= int(previous["release"]),
                            "CANN downgrade is not supported by env update")
    # Toolkit-only upgrades retain the old OPS receipt; check supported major compatibility.
    components = previous.get("components") or [
        {"component": kind, "name": name, "version": previous["version"], "release": previous["release"],
         "sha256": next((v["sha256"] for v in previous.get("artifacts", []) if v["name"] == name), None)}
        for kind, name in previous.get("packages", [])]
    retained = [v for v in components if v["component"] != "toolkit" and not (with_ops and v["component"] == "ops")]
    if release["release"] == previous.get("release") and (not with_ops or any(
            v["component"] == "ops" and v["release"] == release["release"] and v["name"] == name for v in components)):
        return None
    if not with_ops:
        ops = next((v for v in retained if v["component"] == "ops"), None)
        if ops:
            old_major = int(ops["version"].split(".")[0])
            new_major = int(release["version"].split(".")[0])
            workflow_common.require(abs(new_major - old_major) <= 1,
                                    "retained OPS is incompatible with target Toolkit; use --cann DATE --ops")
    report = download_release(c, job, release, hardware)
    report["retained_components"] = retained
    report["ops_upgrade"] = any(v["component"] == "ops" for v in components)
    return report


def download_release(c, job, release, hardware, pins=None):
    root = Path(c["runtime"]["install_root"])
    directory = Path(tempfile.mkdtemp(prefix="af-cann-" + job.name + "-", dir="/tmp"))
    artifacts = []
    report = dict(
        release,
        hardware=hardware,
        artifacts=artifacts,
        install_root=str(root),
        download_dir=str(directory),
        state="downloading",
    )
    workflow_common.update_record(job / "result.json", cann=report)
    try:
        for component, name in release["packages"]:
            path = directory / name
            pinned = next((v for v in (pins or {}).get("components", []) if v["name"] == name), None)
            base = release["base_url"]
            if pinned:
                base = https(c["cann"]["repository_url"]).rstrip("/") + "/" + pinned["release"] + "/"
            address = urllib.parse.urljoin(base, name)
            with (
                urllib.request.urlopen(https(address), timeout=60) as response,
                path.with_suffix(".part").open("xb") as output,
            ):
                https(response.url)
                shutil.copyfileobj(response, output)
            path.with_suffix(".part").replace(path)
            if pins:
                hashes = {item["name"]: item["sha256"] for item in pins["artifacts"]}
                workflow_common.require(
                    hashes.get(name) == workflow_common.sha(path),
                    "frozen CANN package hash mismatch: " + name,
                )
            workflow_common.run(["bash", path, "--check"])
            artifacts.append(
                {
                    "component": component,
                    "path": str(path),
                    "sha256": workflow_common.sha(path),
                    "release": pinned["release"] if pinned else release["release"],
                    "version": pinned["version"] if pinned else release["version"],
                }
            )
            workflow_common.update_record(job / "result.json", cann=report)
    except BaseException as error:
        report.update(state="download_failed", error=str(error))
        workflow_common.update_record(job / "result.json", cann=report)
        raise
    return report


def install_release(c, job, report, upgrade=False):
    root = Path(c["runtime"]["install_root"])
    state = root / ".af-cann-bootstrap.json"
    artifacts = report["artifacts"]
    directory = Path(report["download_dir"])
    report["state"] = "installing"
    workflow_common.update_record(job / "result.json", cann=report)
    workflow_common.write(state, report)
    workflow_common.phase(job, "cann_install")
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"}
    }
    try:
        for item in artifacts:
            command = ["bash", item["path"], "--install-path=" + str(root), "--quiet"]
            if item["component"] == "toolkit":
                command.append("--upgrade" if upgrade else "--full")
            elif item["component"] == "ops":
                command += ["--upgrade" if upgrade and report.get("ops_upgrade") else "--install", "--type=toolkit"]
            else:
                command += ["--install", "--whitelist=all"]
            workflow_common.run(command, env=env, cwd=directory)
            if item["component"] == "toolkit":
                workflow_common.require(
                    ready(root), "Toolkit did not create a complete CANN environment"
                )
                if upgrade:
                    info = dict(line.split("=", 1) for line in (root / "cann/compiler/version.info").read_text().splitlines() if "=" in line)
                    workflow_common.require(info.get("Version") == report["version"].split("~")[0],
                                            "Toolkit version after upgrade differs from selected package")
                script = 'source "$1" && "$2" -c "import os,json; print(json.dumps(dict(os.environ)))"'
                env = json.loads(
                    workflow_common.capture(
                        [
                            "bash",
                            "-c",
                            script,
                            "cann-bootstrap",
                            root / "cann/set_env.sh",
                            sys.executable,
                        ],
                        env=env,
                    ).splitlines()[-1]
                )
        report["components"] = [*report.get("retained_components", []), *[
            {"component": item["component"], "version": item.get("version", report["version"]), "release": item.get("release", report["release"]),
             "name": Path(item["path"]).name, "sha256": item["sha256"]} for item in artifacts]]
        report["components"].sort(key=lambda v: {"toolkit": 0, "ops": 1, "nnal": 2}[v["component"]])
        report["state"] = "ready"
        workflow_common.write(state, report)
        workflow_common.update_record(job / "result.json", cann=report)
    except BaseException:
        report["state"] = "invalid"
        workflow_common.write(state, report)
        workflow_common.update_record(job / "result.json", cann=report)
        raise
    cleanup_packages(directory, artifacts, report)
    workflow_common.write(state, report)
    workflow_common.update_record(job / "result.json", cann=report)


def cleanup_packages(directory, artifacts, report):
    """Remove only this installation's downloaded run files; retain installer logs."""
    removed = []
    try:
        for item in artifacts:
            path = Path(item["path"])
            workflow_common.require(
                path.parent == directory and path.suffix == ".run",
                "unexpected CANN cleanup path",
            )
            path.unlink(missing_ok=True)
            removed.append(str(path))
        if not any(directory.iterdir()):
            directory.rmdir()
        report["cleanup"] = {"state": "complete", "removed": removed}
    except OSError as error:
        report["cleanup"] = {"state": "failed", "removed": removed, "error": str(error)}
        print("CANN installed; package cleanup failed: " + str(error), file=sys.stderr)
