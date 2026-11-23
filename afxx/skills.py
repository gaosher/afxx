"""Install bundled skills for local agents without workflow configuration."""
import shutil
import tempfile
from importlib.resources import files
from pathlib import Path

from afxx import common

SKILLS = ("afx-issue-repair", "afxxsummary")


def bundled_skills():
    root = files("afxx").joinpath("bundled_skills")
    if root.is_dir():
        return root
    # Editable/source installs share the repository's single skill source.
    root = Path(__file__).resolve().parent.parent / "skills"
    common.require_code(root.is_dir(), "SKILLS_NOT_PACKAGED", "AFXX skill resources are missing; reinstall afxx")
    return root


def contents(root):
    result = {}
    for child in root.iterdir():
        common.require_code(not (isinstance(child, Path) and child.is_symlink()),
                            "SKILL_CONFLICT", f"Skill contains a symbolic link: {child}")
        if child.is_dir():
            result.update({child.name + "/" + key: value for key, value in contents(child).items()})
        else:
            result[child.name] = child.read_bytes()
    return result


def install():
    source = bundled_skills()
    destination = Path.home() / ".agents" / "skills"
    destination.mkdir(parents=True, exist_ok=True)
    with common.lock(destination / ".afxx-install.lock"):
        payloads = {name: contents(source.joinpath(name)) for name in SKILLS}
        for name, payload in payloads.items():
            target = destination / name
            common.require_code(not target.is_symlink(), "SKILL_CONFLICT", f"Skill target is a symbolic link: {target}")
            if target.exists():
                common.require_code(target.is_dir() and contents(target) == payload,
                                    "SKILL_CONFLICT", f"Existing skill differs: {target}; move it aside before reinstalling")
        installed = []
        for name, payload in payloads.items():
            target = destination / name
            reused = target.exists()
            if not reused:
                staging = Path(tempfile.mkdtemp(prefix=".afxx-", dir=destination))
                try:
                    for relative, content in payload.items():
                        path = staging / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    staging.rename(target)
                finally:
                    if staging.exists():
                        shutil.rmtree(staging)
            installed.append({"name": name, "path": str(target), "reused": reused})
    return {"agent": "codex", "skills": installed}
