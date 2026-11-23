from pathlib import Path

import pytest
import yaml

from afxx import common, config_save, jobs


@pytest.mark.parametrize('text', [
    '# header\nlocal: {source_dir: /local/source}\nbuild: {lld_bin: /usr/bin/ld.lld, llvm_build_dir: /mnt/llvm}\nconnection: {environment: test}\nworkflow_root: /mnt/remote\npython_env:\n  afx:\n    version: latest # keep comment\n# footer\n',
    'local: {source_dir: /local/source}\nbuild: {lld_bin: /usr/bin/ld.lld, llvm_build_dir: /mnt/llvm}\nconnection: {environment: test}\nworkflow_root: /mnt/remote\npython_env: {afx: {version: latest}} # keep comment\n',
    'local: {source_dir: /local/source}\nbuild: {lld_bin: /usr/bin/ld.lld, llvm_build_dir: /mnt/llvm}\nconnection: {environment: test}\nworkflow_root: /mnt/remote # keep comment\n',
])
def test_render_preserves_comments_and_unrelated_values(text):
    result = config_save.render(text, {'python_env': {'afx': {'version': '0.3.0'}}})
    assert '# keep comment' in result
    original = yaml.safe_load(text)
    expected = dict(original, python_env={'afx': {'version': '0.3.0'}})
    assert yaml.safe_load(result) == expected
    assert config_save.render(result, {'python_env': {'afx': {'version': '0.3.0'}}}) == result


def test_torch_pin_in_explicit_requirements_and_advanced_daily_fields():
    text = '''local: {source_dir: /local/source}
build: {lld_bin: /usr/bin/ld.lld, llvm_build_dir: /mnt/llvm}
connection: {environment: test}
workflow_root: /mnt/remote
python_env:
  stable_requirements:
    - torch==2.10.0+cpu # torch
    - numpy # keep
  torch_npu: {version: 2.10.0, date: latest, sha256: old}
'''
    updates = {'python_env': {'torch_version': '2.11.0', 'torch_npu': {'version': '2.11.0', 'date': '20261123', 'sha256': 'a'*64}}}
    result = config_save.render(text, updates)
    value = yaml.safe_load(result)['python_env']
    assert value['stable_requirements'] == ['torch==2.11.0', 'numpy']
    assert 'torch_version' not in value
    assert value['torch_npu']['sha256'] == 'a'*64
    assert '# torch' in result and '# keep' in result


@pytest.mark.parametrize('text', ['a: &x {b: 1}\nc: *x\n', 'a: 1\na: 2\n'])
def test_ambiguous_yaml_rejected_before_submission(tmp_path, text):
    path=tmp_path/'config.yaml'
    path.write_text(text)
    with pytest.raises(common.WorkflowError, match='YAML'):
        config_save.prepare(path, {'afx': 'latest'})


@pytest.fixture
def pending(cfg, tmp_path):
    path=tmp_path/'user.yaml'
    path.write_text('local: {source_dir: /local/source}\nbuild: {lld_bin: /usr/bin/ld.lld, llvm_build_dir: /mnt/llvm}\nconnection: {environment: test}\nworkflow_root: /mnt/remote\npython_env: {afx: {version: latest}} # keep\n')
    job=Path(cfg['local']['state_dir'])/'update-job'
    job.mkdir(parents=True)
    common.write(job/'config.yaml', cfg)
    request={'command': 'update', 'save_config': config_save.prepare(path, {'afx': 'latest'})}
    common.write(job/'request.json', request)
    report={'config_updates': {'python_env': {'afx': {'version': '0.3.0'}}}}
    return path, job, report


def test_success_atomic_write_and_repeated_status_reuses_receipt(cfg, pending, monkeypatch):
    path, job, report=pending
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {'result.json': report})
    first=jobs.complete_config_save(cfg, job.name, {'state': 'success'})
    assert first['config_save']['path'] == str(path)
    assert yaml.safe_load(path.read_text())['python_env']['afx']['version'] == '0.3.0'
    monkeypatch.setattr(jobs, 'query_job', lambda *a: pytest.fail('receipt should be reused'))
    assert jobs.complete_config_save(cfg, job.name, {'state': 'success'}) == first


@pytest.mark.parametrize('state', ['running', 'failed'])
def test_incomplete_update_never_writes(cfg, pending, monkeypatch, state):
    path, job, _=pending
    old=path.read_bytes()
    monkeypatch.setattr(jobs, 'query_job', lambda *a: pytest.fail('must not query'))
    assert jobs.complete_config_save(cfg, job.name, {'state': state}) == {'state': state}
    assert path.read_bytes() == old


def test_user_edit_is_not_overwritten(cfg, pending, monkeypatch):
    path, job, report=pending
    path.write_text(path.read_text()+'# edited while waiting\n')
    old=path.read_bytes()
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {'result.json': report})
    with pytest.raises(common.WorkflowError) as error:
        jobs.complete_config_save(cfg, job.name, {'state': 'success'})
    assert error.value.code == 'CONFIG_SAVE_CONFLICT'
    assert error.value.data['environment_state'] == 'success'
    assert path.read_bytes() == old


def test_retry_after_replace_before_receipt_is_safe(cfg, pending, monkeypatch):
    path, job, report=pending
    path.write_text(config_save.render(path.read_text(), report['config_updates']))
    monkeypatch.setattr(jobs, 'query_job', lambda *a: {'result.json': report})
    assert jobs.complete_config_save(cfg, job.name, {'state': 'success'})['config_save']['state'] == 'saved'


def test_cli_save_intent_dry_run_and_empty_selection(cfg, pending, monkeypatch):
    import sys
    from afxx import cli
    path, _, _ = pending
    original = path.read_bytes()
    seen = []
    cfg['python_env'] = {}
    monkeypatch.setattr(cli.workflow_config, 'config', lambda _: cfg)
    monkeypatch.setattr(jobs, 'submit', lambda c, r, k, d: seen.append(r) or {'job': 'dry', 'dry_run': True})
    argv = ['afxx', 'env', 'update', '--config', str(path), '--save-config', '--dry-run']
    monkeypatch.setattr(sys, 'argv', argv + ['--afx', 'latest'])
    cli.main()
    assert seen[0]['save_config']['path'] == str(path)
    assert path.read_bytes() == original
    monkeypatch.setattr(sys, 'argv', argv)
    with pytest.raises(common.WorkflowError, match='requires an update component'):
        cli.main()
    assert len(seen) == 1
