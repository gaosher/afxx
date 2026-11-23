import json
import subprocess
import sys
from pathlib import Path

import pytest

from afxx import common, skills


def test_install_and_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    first = skills.install()
    assert [entry["name"] for entry in first["skills"]] == list(skills.SKILLS)
    for entry in first["skills"]:
        assert not entry["reused"]
        assert skills.contents(Path(entry["path"])) == skills.contents(skills.bundled_skills().joinpath(entry["name"]))
    assert all(entry["reused"] for entry in skills.install()["skills"])


def test_conflict_preserves_both_skills(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    target = tmp_path / ".agents/skills/afx-issue-summary"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("local changes")
    with pytest.raises(common.WorkflowError) as error:
        skills.install()
    assert error.value.code == "SKILL_CONFLICT"
    assert (target / "SKILL.md").read_text() == "local changes"
    assert not (target.parent / "afx-issue-repair").exists()


def test_symlink_target_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".agents/skills"
    root.mkdir(parents=True)
    (root / "afx-issue-repair").symlink_to(tmp_path / "missing")
    with pytest.raises(common.WorkflowError):
        skills.install()
    assert (root / "afx-issue-repair").is_symlink()


def test_cli_requires_no_workflow_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    proc = subprocess.run([sys.executable, "-m", "afxx.cli", "skills", "install", "--agent", "codex"],
                          capture_output=True, text=True, check=True)
    result = json.loads(proc.stdout)
    assert result["ok"] and result["command"] == "skills install"
    assert len(result["data"]["skills"]) == 2
