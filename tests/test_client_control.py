import json
from pathlib import Path

import pytest

from afxx.afx import managed_client as m
from afxx.afx import launcher
from afxx import cli


@pytest.fixture
def client(tmp_path, monkeypatch):
    token = tmp_path / 'token'
    token.write_text('secret-token')
    home = tmp_path / 'client'
    home.mkdir()
    (home/'config.json').write_text(json.dumps({'source_id':'stable-identity'}))
    (home/'client.json').write_text(json.dumps(dict(mode='managed', endpoint='https://example.test', devices=['npu:0'], token='secret-token')))
    settings = dict(home=str(home), token_file=str(token), endpoint='https://example.test', devices=['npu:0'], timeout=5)
    monkeypatch.setattr(m, 'live_clients', lambda: [])
    monkeypatch.setattr(m, 'alive', lambda pid: False)
    monkeypatch.setattr(m.time, 'sleep', lambda seconds: None)
    return settings


class Daemon:
    def __init__(self, state=None):
        self.state = state
        self.calls = []
        self.draining = 0

    def run(self, argv):
        action = argv[2]
        self.calls.append(action)
        if action == 'stop':
            self.state = 'draining'
            self.draining = 2
        elif action == 'start':
            self.state = 'running'
            return {'state':'running', 'run_ids':['run']}

    def legacy(self, argv):
        self.calls.append('daemon-' + argv[1])
        if argv[1] == 'stop':
            assert self.state == 'idle'
            self.state = None
            return {'stopping':True}
        if self.state is None:
            raise FileNotFoundError()
        if self.state == 'draining':
            self.draining -= 1
            if self.draining == 0:
                self.state = 'idle'
        return dict(pid=99999999, runs=[] if self.state=='idle' else [dict(id='run',state=self.state)], active_operation=None)


@pytest.mark.parametrize('initial,expected', [(None,'stopped'),('idle','idle'),('running','running'),('draining','draining')])
def test_status_never_starts(client, initial, expected):
    d = Daemon(initial)
    d.draining = 3
    result = m.execute(dict(client, action='status'), d.run, d.legacy)
    assert result['state'] == expected
    assert result['machine_id'] == 'stable-identity'
    assert d.calls == ['daemon-status']
    assert 'secret-token' not in json.dumps(result)


def test_stop_drains_then_stops_daemon_and_is_idempotent(client):
    d = Daemon('running')
    assert m.execute(dict(client, action='stop'), d.run, d.legacy)['state'] == 'stopped'
    assert d.calls.index('stop') < d.calls.index('daemon-stop')
    d.calls.clear()
    assert m.execute(dict(client, action='stop'), d.run, d.legacy)['state'] == 'stopped'
    assert d.calls == ['daemon-status']


def test_restart_drains_before_start_and_preserves_identity(client):
    d = Daemon('running')
    result = m.execute(dict(client, action='restart'), d.run, d.legacy)
    assert d.calls.index('daemon-stop') < d.calls.index('start')
    assert result['state'] == 'running' and result['machine_id'] == 'stable-identity'


def test_restart_reuses_afx_configuration(client):
    d = Daemon('running')
    before = (Path(client['home'])/'client.json').read_text()
    result = m.execute(dict(client, action='restart', devices=['npu:1'],
                            endpoint='https://different.test', token_file='/missing/token'), d.run, d.legacy)
    assert result['state'] == 'running'
    assert (Path(client['home'])/'client.json').read_text() == before
    assert 'init' not in d.calls


def test_restart_without_daemon_reports_error(client):
    d = Daemon()
    with pytest.raises(m.ClientError) as error:
        m.execute(dict(client, action='restart'), d.run, d.legacy)
    assert error.value.code == 'PLATFORM_CLIENT_NOT_RUNNING'
    assert 'start' not in d.calls


def test_stop_timeout_does_not_kill(client, monkeypatch):
    d = Daemon('running')
    ticks = iter([0, 10])
    monkeypatch.setattr(m.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(m.ClientError) as error:
        m.execute(dict(client, action='stop', timeout=1), d.run, d.legacy)
    assert error.value.code == 'PLATFORM_CLIENT_TIMEOUT'
    assert 'daemon-stop' not in d.calls


@pytest.mark.parametrize('action', ['start', 'stop', 'restart', 'status'])
def test_multiple_clients_fail_before_actions(client, monkeypatch, action):
    monkeypatch.setattr(m, 'live_clients', lambda: [{'pid':1}, {'pid':2}])
    d = Daemon()
    with pytest.raises(m.ClientError) as error:
        m.execute(dict(client, action=action), d.run, d.legacy)
    assert error.value.code == 'PLATFORM_CLIENT_CONFLICT'
    assert not d.calls


def test_status_without_credentials_or_cann(platform_cfg, monkeypatch):
    monkeypatch.setattr(launcher,'selected_client',lambda: None)
    monkeypatch.setattr(launcher.runtime,'environment',lambda c: pytest.fail('must not load CANN'))
    monkeypatch.setattr(launcher.w,'capture',lambda *a,**kw: '{"state":"uninitialized"}')
    home=Path(platform_cfg['remote']['state_dir']).parent/'afx-client'
    monkeypatch.setattr(launcher,'client_home',lambda c:home)
    assert launcher.control(platform_cfg,'status')['state']=='stopped'
    assert not home.exists()


@pytest.mark.parametrize('action', ['start','status','stop','restart'])
def test_cli_client_dispatch(platform_cfg, monkeypatch, action):
    monkeypatch.setattr(cli.sys,'argv',['afxx','client',action,'--timeout','20'])
    monkeypatch.setattr(cli.workflow_config,'config',lambda p: platform_cfg)
    monkeypatch.setattr(cli.workflow_jobs,'client_control',lambda c,a,t: {'action':a,'timeout':t})
    assert cli.main()=={'action':action,'timeout':20}
    assert cli.response('client-'+action, {})['command']=='client '+action


def test_home_uses_remote_afx_resolver(platform_cfg, monkeypatch):
    calls=[]
    monkeypatch.setattr(launcher.w,'capture',lambda argv,**kw: calls.append(argv) or '"/home/developer/.local/share/autofusex"')
    assert str(launcher.client_home(platform_cfg))=='/home/developer/.local/share/autofusex'
    assert 'data_home()' in calls[0][-1]
    assert platform_cfg['remote']['state_dir'] not in calls[0][-1]


def test_existing_client_uses_saved_credential(client):
    d=Daemon('running')
    result=m.execute(dict(client,action='start',token_file=None),d.run,d.legacy)
    assert result['state']=='running'
    assert 'init' not in d.calls


def test_uninitialized_status_does_not_create_home(client):
    missing=Path(client['home'])/'absent'
    d=Daemon()
    assert m.execute(dict(client,home=str(missing),action='status'),d.run,d.legacy)['state']=='uninitialized'
    assert not missing.exists()


def test_process_discovery_counts_supervisor_and_child_once(tmp_path, monkeypatch):
    root = tmp_path/'proc'
    root.mkdir()
    def process(pid, parent, action, home='/live', legacy=False):
        p = root/str(pid)
        p.mkdir()
        args = ['python', '-m', 'afx'] + (['platform', 'client', 'run'] if legacy else ['daemon', action])
        (p/'cmdline').write_bytes(('\0'.join(args)+'\0').encode())
        (p/'environ').write_text('AUTOFUSEX_HOME='+home+'\0')
        (p/'stat').write_text(f'{pid} (python) S {parent} 0 0')
    process(10, 1, 'supervise')
    process(11, 10, 'run')
    monkeypatch.setattr(m, 'Path', lambda value: root if value == '/proc' else Path(value))
    monkeypatch.setattr(m, 'alive', lambda pid: True)
    clients = m.live_clients()
    assert [c['pid'] for c in clients] == [11]
    process(12, 1, 'run')
    assert len(m.live_clients()) == 2  # Same home does not make two daemons one client.
    with pytest.raises(m.ClientError, match='multiple live'):
        m.selected_client()


def test_unknown_or_legacy_client_fails_closed(monkeypatch):
    for client in ({'legacy': True, 'home': '/live'}, {'legacy': False, 'home': None}):
        monkeypatch.setattr(m, 'live_clients', lambda: [client])
        with pytest.raises(m.ClientError) as error:
            m.selected_client()
        assert error.value.code == 'PLATFORM_CLIENT_UNTRACKED'
