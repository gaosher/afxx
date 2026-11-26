import json
from pathlib import Path
import sys

import pytest
import tomlkit

from afxx import common, skills, integration, cli


def test_skills_upgrade_backups_and_removes_obsolete_files(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    skills.install()
    target = tmp_path/'.agents/skills/afx-issue-repair'
    (target/'SKILL.md').write_text('old')
    (target/'obsolete.txt').write_text('old resource')
    result = skills.install(upgrade=True)
    entry = result['skills'][0]
    assert Path(entry['backup'], 'SKILL.md').read_text() == 'old'
    assert not (target/'obsolete.txt').exists()
    assert skills.contents(target) == skills.contents(skills.bundled_skills().joinpath('afx-issue-repair'))
    assert all(e['reused'] for e in skills.install(upgrade=True)['skills'])


def test_mcp_upgrade_preserves_settings(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    path = tmp_path/'config.toml'
    original = '''# keep comment
model = "test"
[mcp_servers.afxx]
command = "/old/afxx"
args = ["mcp", "--config", "/custom/config.yaml"]
enabled = false
startup_timeout_sec = 90
[mcp_servers.afxx.env]
EXAMPLE = "keep"
[mcp_servers.other]
command = "other"
'''
    path.write_text(original)
    result = integration.upgrade_mcp()
    assert Path(result['backup']).read_text() == original
    doc = tomlkit.loads(path.read_text())
    assert '# keep comment' in path.read_text()
    server = doc['mcp_servers']['afxx']
    assert server['command'] == sys.executable
    assert server['args'] == ['-m','afxx.cli','mcp','--config','/custom/config.yaml']
    assert server['enabled'] is False and server['env']['EXAMPLE'] == 'keep'
    assert doc['mcp_servers']['other']['command'] == 'other'
    assert result['reconnect_required']
    assert not integration.upgrade_mcp()['changed']


def test_mcp_http_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    p=tmp_path/'config.toml';p.write_text('[mcp_servers.afxx]\nurl="https://example.test"\n')
    with pytest.raises(common.WorkflowError, match='HTTP'):
        integration.upgrade_mcp()
    assert 'url=' in p.read_text()


@pytest.mark.parametrize('group', ['skills', 'mcp'])
def test_upgrade_cli(group, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path/'codex'))
    monkeypatch.setattr(sys, 'argv', ['afxx', group, 'upgrade', '--agent', 'codex'])
    assert cli.cli() == 0
    result=json.loads(capsys.readouterr().out)
    assert result['ok'] and result['command'] == group+' upgrade'
