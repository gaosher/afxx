"""Explicit updates preserve unselected components and preflight before installation."""
import pytest

from afxx import cli, common
from afxx.environment import cann
from afxx.environment import python as env
from test_af_python_env import env_cfg as env_cfg, managed


@pytest.mark.parametrize('selection', [
    {'ops': True}, {'cann': '20260230'}, {'torch_npu': 'yesterday'},
    {'torch': 'latest'}, {'afx': '--pre'},
])
def test_invalid_update_selection(selection):
    with pytest.raises(common.WorkflowError) as error:
        env.validate_selection(selection)
    assert error.value.code == 'INVALID_ARGUMENT'


def test_torch_change_requires_matching_daily_and_ignores_init_versions(env_cfg, tmp_path):
    before, _ = managed(env_cfg, tmp_path)
    with pytest.raises(common.WorkflowError, match='requires --torch-npu'):
        env.update_config(env_cfg, before, {'torch': '2.11.0'})
    result = env.update_config(env_cfg, before, {'torch': '2.11.0', 'torch_npu': 'latest'})
    assert result['python_env']['torch_npu']['version'] == '2.11.0'
    assert result['python_env']['torch_npu']['sha256'] is None
    assert 'torch==2.11.0' in result['python_env']['stable_requirements']
    assert result['python_env']['afx'] == before['artifacts']['afx']['source']
    assert env_cfg['python_env']['torch_npu']['version'] == '2.10.0'


def test_update_without_selection_only_reports_actual_versions(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    current = dict(before['packages'], autofusex='0.3.0')
    monkeypatch.setattr(env, 'packages', lambda c: current)
    monkeypatch.setattr(env, 'artifact', lambda *a: pytest.fail('must not download'))
    monkeypatch.setattr(env, 'sync', lambda *a: pytest.fail('must not install'))
    env.update(env_cfg, job, {})
    assert common.read(job/'result.json')['installed']['packages']['autofusex'] == '0.3.0'
    assert common.read(env.state_path(env_cfg)) == before


@pytest.fixture
def cann_update(tmp_path, monkeypatch):
    root = tmp_path/'Ascend'
    (root/'cann/compiler').mkdir(parents=True)
    (root/'cann/set_env.sh').touch()
    (root/'cann/compiler/version.info').write_text('Version=9.3.0\n')
    c = {'runtime': {'install_root': str(root)}, 'cann': {'ops': 'A3', 'nnal': True}}
    old = {'version': '9.3.0', 'release': '2026111201', 'components': [
        {'component': 'ops', 'version': '9.3.0', 'release': '2026111201', 'name': 'old-ops.run', 'sha256': 'old'}]}
    release = {'release': '2026112301', 'version': '9.3.0', 'base_url': 'https://packages.invalid/2026112301/',
               'packages': [('toolkit', 'toolkit.run')]}
    def select(cfg, family, architecture):
        assert not cfg['nnal'] and family is None
        return dict(release, packages=list(release['packages']))
    monkeypatch.setattr(cann, 'select_release', select)
    monkeypatch.setattr(cann.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(cann, 'download_release', lambda c,j,r,h: dict(r, artifacts=[], hardware=h))
    return c, old, tmp_path, release


def test_toolkit_only_preserves_ops_receipt(cann_update):
    c, old, job, _ = cann_update
    result = cann.prepare_update(c, job, 'latest', False, old)
    assert result['packages'] == [('toolkit', 'toolkit.run')]
    assert result['retained_components'] == old['components']


def test_ops_must_come_from_selected_toolkit_batch(cann_update, monkeypatch):
    c, old, job, _ = cann_update
    monkeypatch.setattr(cann, 'listing', lambda url: [])
    with pytest.raises(ValueError, match='matching OPS package missing'):
        cann.prepare_update(c, job, 'latest', True, old)
    name = 'Ascend-cann-A3-ops_9.3.0_linux-aarch64.run'
    monkeypatch.setattr(cann, 'listing', lambda url: [name])
    result = cann.prepare_update(c, job, 'latest', True, old)
    assert result['packages'][-1] == ('ops', name)
    assert result['retained_components'] == []


@pytest.mark.parametrize('ops', [False, True])
def test_cann_upgrade_commands_and_cleanup(cann_update, monkeypatch, ops):
    c, old, job, release = cann_update
    directory = job/'downloads'
    directory.mkdir()
    components = ['toolkit', 'ops'] if ops else ['toolkit']
    artifacts = []
    for component in components:
        path = directory/(component+'.run')
        path.write_bytes(b'package')
        artifacts.append({'path': str(path), 'component': component, 'sha256': 'hash'})
    report = dict(release, artifacts=artifacts, download_dir=str(directory), ops_upgrade=True,
                  retained_components=[] if ops else old['components'])
    calls=[]
    monkeypatch.setattr(common, 'run', lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(common, 'capture', lambda *a, **kw: '{}')
    cann.install_release(c, job, report, upgrade=True)
    assert all('--upgrade' in call and '--full' not in call for call in calls)
    if ops:
        assert '--type=toolkit' in calls[1]
    assert report['state'] == 'ready'
    assert len(report['components']) == 2
    assert not list(directory.glob('*.run'))


def test_cann_started_failure_does_not_claim_rollback(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    monkeypatch.setattr(env, 'packages', lambda c: before['packages'])
    monkeypatch.setattr(cann, 'prepare_update', lambda *a: {'release': '2026112301'})
    monkeypatch.setattr(cann, 'install_release', lambda *a, **kw: (_ for _ in ()).throw(ValueError('installer failed')))
    monkeypatch.setattr(env, 'sync', lambda *a: pytest.fail('no Python changes'))
    with pytest.raises(ValueError, match='installer failed'):
        env.update(env_cfg, job, {'cann': 'latest'})
    assert common.read(env.state_path(env_cfg))['state'] == 'invalid'
    assert common.read(job/'result.json')['rollback']['state'] == 'unavailable'


def test_cli_freezes_updates_and_rejects_ops_without_submission(env_cfg, monkeypatch):
    import sys
    from afxx import jobs
    monkeypatch.setattr(cli.workflow_config, 'config', lambda path: env_cfg)
    seen=[]
    monkeypatch.setattr(jobs, 'local_preflight', lambda *args: None)
    monkeypatch.setattr(jobs, 'submit', lambda c,r,k,d: seen.append(r) or {'job': 'test', 'dry_run': True})
    monkeypatch.setattr(sys, 'argv', ['afxx', 'env', 'update', '--dry-run', '--afx', 'latest', '--cann', '20261123', '--ops'])
    cli.main()
    assert seen[0]['updates'] == {'afx': 'latest', 'cann': '20261123', 'ops': True}
    monkeypatch.setattr(sys, 'argv', ['afxx', 'env', 'update', '--ops'])
    with pytest.raises(common.WorkflowError, match='--ops requires --cann'):
        cli.main()
    assert len(seen) == 1


def test_mcp_forwards_explicit_update_selection(monkeypatch):
    pytest.importorskip('mcp')
    from test_mcp_tools import call
    from afxx import mcp_server, mcp_operations
    seen=[]
    monkeypatch.setattr(mcp_operations, 'submit', lambda command,args,path,key: seen.append(args) or {'ok': True})
    call(mcp_server.create_server('/config'), 'afxx_run', {
        'command': 'env update', 'arguments': ['--torch', '2.11.0', '--torch-npu', 'latest'], 'request_id': 'update-1'})
    assert seen == [['env', 'update', '--config', '/config', '--torch', '2.11.0', '--torch-npu', 'latest', '--detach', '--request-id', 'update-1']]


def test_version_query_does_not_wait_for_fuzz_execution_locks(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    monkeypatch.setattr(env, 'packages', lambda c: before['packages'])
    monkeypatch.setattr(env.workflow_runtime, 'venv_lock', lambda c: pytest.fail('query must not take execution locks'))
    monkeypatch.setattr(env.workflow_runtime, 'check_idle', lambda *a: pytest.fail('query must not require idle'))
    assert env.execute(env_cfg, job, 'update', {}) == 0
    assert common.read(job/'result.json')['state'] == 'success'


def test_detached_update_status_exposes_versions_without_full_manifest():
    from afxx.job_view import status_view
    data = status_view({'task_type': 'update', 'state': 'success', 'result.json': {
        'installed': {'packages': {'torch': '2.11.0'}, 'cann': {'version': '9.3.0', 'date': '20261123', 'artifacts': ['large']}},
        'components': ['torch', 'torch-npu']}})
    assert data['update'] == {'packages': {'torch': '2.11.0'}, 'cann': {'version': '9.3.0', 'date': '20261123'}, 'components': ['torch', 'torch-npu']}


def test_frozen_cann_downloads_retained_ops_from_its_original_batch(cann_update, monkeypatch):
    import hashlib
    import io
    from pathlib import Path
    c, _, job, release = cann_update
    monkeypatch.undo()  # Exercise real downloads rather than the planning fixture stub.
    c['cann']['repository_url'] = 'https://packages.invalid/'
    digest = hashlib.sha256(b'package').hexdigest()
    components = [
        {'component': 'toolkit', 'name': 'toolkit.run', 'version': '9.3.0', 'release': '2026112301', 'sha256': digest},
        {'component': 'ops', 'name': 'ops.run', 'version': '9.2.0', 'release': '2026100101', 'sha256': digest}]
    release['packages'] = [(v['component'], v['name']) for v in components]
    directory = job/'downloads'
    directory.mkdir()
    monkeypatch.setattr(cann.tempfile, 'mkdtemp', lambda **kw: str(directory))
    addresses=[]
    def download(address, **kw):
        addresses.append(address)
        value=io.BytesIO(b'package')
        value.url=address
        return value
    monkeypatch.setattr(cann.urllib.request, 'urlopen', download)
    monkeypatch.setattr(common, 'run', lambda *a, **kw: None)
    pins = {'components': components, 'artifacts': [{'name': v['name'], 'sha256': digest} for v in components]}
    report = cann.download_release(c, job, release, [], pins)
    assert addresses == ['https://packages.invalid/2026112301/toolkit.run', 'https://packages.invalid/2026100101/ops.run']
    assert report['artifacts'][1]['release'] == '2026100101'
    assert all(Path(v['path']).read_bytes() == b'package' for v in report['artifacts'])


def test_torch_update_resolves_matching_npu_and_dependency_changes(env_cfg, tmp_path, monkeypatch):
    before, job = managed(env_cfg, tmp_path)
    inventory = dict(before['packages'])
    monkeypatch.setattr(env, 'packages', lambda c: dict(inventory))
    candidate = dict(before['artifacts']['torch_npu'], sha256='new', source={
        **before['artifacts']['torch_npu']['source'], 'version': '2.11.0', 'date': '20261123'})
    def artifact(c, name, job):
        assert name == 'torch_npu' and c['python_env'][name]['version'] == '2.11.0'
        return candidate
    monkeypatch.setattr(env, 'artifact', artifact)
    locked=job/'pylock.toml'
    locked.write_text('new lock')
    def resolve(c, artifacts, job, previous, changed):
        assert previous is None and set(changed) == {'torch', 'torch-npu'}
        assert artifacts['afx'] == before['artifacts']['afx']
        return locked
    monkeypatch.setattr(env, 'resolve', resolve)
    monkeypatch.setattr(env, 'sync', lambda *a: inventory.update(torch='2.11.0', **{'torch-npu': '2.11.0', 'sympy': 'new'}))
    monkeypatch.setattr(env, 'verify', lambda *a: None)
    monkeypatch.setattr(common, 'capture', lambda *a, **kw: 'test')
    env.update(env_cfg, job, {'torch': '2.11.0', 'torch_npu': 'latest'})
    after=common.read(env.state_path(env_cfg))
    assert after['packages']['torch'] == '2.11.0'
    assert after['packages']['autofusex'] == before['packages']['autofusex']
    assert after['cann'] == before['cann']


def test_save_resolves_actual_versions_and_blocks_mixed_cann(env_cfg, tmp_path, monkeypatch):
    pins = env.config_updates({'afx': 'latest', 'torch_npu': 'latest'},
        {'torch': '2.11.0', 'autofusex': '0.3.1'},
        {'torch_npu': {'source': {'version': '2.11.0', 'date': '20261123', 'sha256': 'a'*64}}}, {})
    assert pins['python_env']['afx']['version'] == '0.3.1'
    assert pins['python_env']['torch_version'] == '2.11.0'
    assert pins['python_env']['torch_npu']['date'] == '20261123'
    before, job = managed(env_cfg, tmp_path)
    common.write(job/'request.json', {'save_config': {'path': '/local/config.yaml'}})
    monkeypatch.setattr(env, 'packages', lambda c: before['packages'])
    monkeypatch.setattr(cann, 'prepare_update', lambda *a: {
        'release': '2026112301', 'retained_components': [{'release': '2026111201'}]})
    monkeypatch.setattr(env, 'sync', lambda *a: pytest.fail('must not mutate Python'))
    monkeypatch.setattr(cann, 'install_release', lambda *a, **k: pytest.fail('must not mutate CANN'))
    with pytest.raises(ValueError, match='mixed CANN'):
        env.update(env_cfg, job, {'cann': 'latest'})
    assert common.read(env.state_path(env_cfg)) == before
