"""Evidence pagination, confinement and truthful AFX provenance."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from afxx.afx import replay
from afxx import jobs, cli, config


@pytest.fixture
def scene(tmp_path):
    job = tmp_path / ("a" * 32)
    job.mkdir()
    results = tmp_path / "results"
    (results / "dump").mkdir(parents=True)
    (job / "result.json").write_text(json.dumps({"results_dir": str(results), "resources": {"dump": True}}))
    return job, results


def test_read_pages_and_changed_file(scene):
    job, results = scene
    path = results / "dump" / "a.mlir"
    path.write_text("0123456789")
    first = replay.read_evidence(job, str(path), 0, 4)
    second = replay.read_evidence(job, str(path), first['next_offset'], 6, first['sha256'])
    assert first['content'] + second['content'] == path.read_text()
    assert second['eof'] and second['next_offset'] is None
    path.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        replay.read_evidence(job, str(path), 4, 6, first['sha256'])


@pytest.mark.parametrize('location', ['outside', 'config', 'link', 'binary'])
def test_read_rejects_non_artifact(scene, location):
    job, results = scene
    if location == 'outside':
        path = job.parent / 'secret.log'; path.write_text('secret')
    elif location == 'config':
        path = results / 'sync.json'; path.write_text('secret')
    elif location == 'link':
        target = job.parent / 'secret.log'; target.write_text('secret')
        path = results / 'dump' / 'link.log'; path.symlink_to(target)
    else:
        path = results / 'dump' / 'binary.txt'; path.write_bytes(b'abc\x00')
    with pytest.raises(ValueError):
        replay.read_evidence(job, str(path), 0, 100)


def test_list_pagination(scene):
    job, results = scene
    for i in range(140):
        (results / 'dump' / f'{i:03}.mlir').write_text('module {}')
    first = replay.collect_evidence(job, results, True)
    second = replay.collect_evidence(job, results, True, first['next_offset'])
    paths = [row['path'] for page in (first, second) for row in page['files']]
    assert len(set(paths)) == len(paths) == 140
    assert second['next_offset'] is None


def test_associations_require_exact_diagnostic_path(scene):
    job, results = scene
    path = results / 'dump' / 'kernel.mlir'; path.write_text('module {}')
    unrelated = results / 'dump' / 'other.mlir'; unrelated.write_text('module {}')
    attempts = [{'id': 'attempt-1', 'seed': 'seed1', 'backend': 'af-mlir', 'status': 'error',
        'shape_results': [{'shape_index': 2, 'correctness': 'error', 'error_stage': 'compile'}],
        'contexts': {'execution': {'diagnostics': json.dumps({'shapes': {'2': {'error': 'failed at ' + str(path)}}})}}},
        {'id': 'attempt-2', 'seed': 'seed2', 'backend': 'af-mlir', 'status': 'error',
         'contexts': {'execution': {'diagnostics': {'error': str(path) + '.other'}}}}]
    data = replay.associate_evidence(replay.collect_evidence(job, results, True), attempts)
    by_path = {row['path']: row for row in data['files']}
    assert by_path[str(path)]['associations'][0]['shape_indices'] == ['2']
    assert len(by_path[str(path)]['associations']) == 1
    assert by_path[str(unrelated)]['association_state'] == 'unassociated'
    assert data['failures'][0]['shape_indices'] == [2]


def test_remote_evidence_protocol_runs_without_installed_forge(scene, monkeypatch):
    job, results = scene
    path = results / 'dump' / 'a.mlir'; path.write_text('test ir')
    def remote(c, argv):
        return json.loads(subprocess.check_output([sys.executable, *argv[1:]], text=True, cwd='/tmp'))
    monkeypatch.setattr(jobs, 'remote', remote)
    cfg = {'remote': {'state_dir': str(job.parent)}}
    assert jobs.query_evidence(cfg, job.name, 'read', path=str(path))['content'] == 'test ir'
    assert jobs.query_evidence(cfg, job.name, 'list')['files'][0]['path'] == str(path)


@pytest.mark.parametrize('action', ['list', 'read'])
def test_cli_routes_evidence_without_platform_claim(scene, monkeypatch, capsys, action):
    job, results = scene
    args = ['afxx', 'evidence', action, '--job-id', job.name]
    if action == 'read': args += ['--path', str(results/'dump/a.mlir'), '--offset', '4']
    monkeypatch.setattr(sys, 'argv', args)
    monkeypatch.setattr(config, 'config', lambda _: {})
    monkeypatch.setattr(jobs, 'local_preflight', lambda command, *_: None)
    monkeypatch.setattr(jobs, 'query_evidence', lambda c, jid, act, **kw: dict(job=jid, action=act, **kw))
    assert cli.cli() == 0
    value = json.loads(capsys.readouterr().out)
    assert value['command'] == 'evidence ' + action
    assert value['data']['action'] == action


def test_explicit_directory_reference_does_not_infer_shape(scene):
    job, results = scene
    folder = results / 'dump' / 'compilation'
    folder.mkdir()
    (folder / 'pass.mlir').write_text('module {}')
    other = results / 'dump' / 'compilation-other'
    other.mkdir()
    (other / 'pass.mlir').write_text('other')
    attempts = [{'id': 'a', 'seed': 'seed', 'backend': 'af-mlir', 'status': 'error',
                 'contexts': {'execution': {'diagnostics': {'error': f'debug trace: {folder}'}}}}]
    data = replay.associate_evidence(replay.collect_evidence(job, results, True), attempts)
    rows = {row['path']: row for row in data['files']}
    link = rows[str(folder / 'pass.mlir')]['associations'][0]
    assert link['basis'] == 'explicit_directory_in_afx_diagnostics'
    assert link['shape_indices'] == [] and link['reference_path'] == str(folder)
    assert rows[str(other / 'pass.mlir')]['association_state'] == 'unassociated'
