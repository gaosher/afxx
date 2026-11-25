import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from afxx import credentials as auth, common, config, cli, jobs


def test_prepare_existing_and_prompt(platform_cfg, monkeypatch):
    p = Path(platform_cfg['platform']['token_file'])
    monkeypatch.setattr(auth.platform, 'call', lambda c,m,p: {'user_id':'test'})
    monkeypatch.setattr(auth.getpass, 'getpass', lambda *_: pytest.fail('unexpected prompt'))
    auth.prepare(platform_cfg)
    assert p.stat().st_mode & 0o777 == 0o600
    assert platform_cfg['platform']['client_token_file'] == platform_cfg['platform']['remote_token_file']
    p.unlink()
    monkeypatch.setattr(auth.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(auth.getpass, 'getpass', lambda *_: 'entered-secret')
    auth.prepare(platform_cfg)
    assert p.read_text().strip() == 'entered-secret'
    assert p.stat().st_mode & 0o777 == 0o600


def test_noninteractive_missing_and_invalid(platform_cfg, monkeypatch):
    path = Path(platform_cfg['platform']['token_file']); path.unlink()
    monkeypatch.setattr(auth.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(common.WorkflowError) as e:
        auth.prepare(platform_cfg)
    assert e.value.code == 'PLATFORM_CREDENTIAL_MISSING'
    assert '删除或注释配置中的整个 platform 段' in str(e.value)
    assert not path.exists()
    path.write_text('invalid secret')
    with pytest.raises(common.WorkflowError) as e:
        auth.read_token(platform_cfg)
    assert 'invalid secret' not in str(e.value)


def test_writer_conflicts_before_writing_and_reuses(tmp_path):
    first=tmp_path/'first'; second=tmp_path/'second';second.write_text('other')
    with pytest.raises(ValueError): auth.install_files([str(first),str(second)],'secret')
    assert not first.exists() and second.read_text()=='other'
    second.unlink();auth.install_files([str(first),str(second)],'secret')
    before=first.stat().st_mtime_ns
    auth.install_files([str(first),str(second)],'secret')
    assert first.stat().st_mtime_ns==before
    assert second.stat().st_mode & 0o777 == 0o600
    link=tmp_path/'link';link.symlink_to(first)
    with pytest.raises(ValueError):auth.install_files([str(link)],'secret')


def test_sync_stdin_remote_writer_and_conflict(platform_cfg, monkeypatch, capsys):
    remote_path=Path(platform_cfg['platform']['remote_token_file']);remote_path.unlink()
    real_run=subprocess.run
    def transport(argv, **kwargs):
        assert 'local-secret' not in ' '.join(argv)
        assert kwargs['stderr']==subprocess.DEVNULL
        assert json.loads(kwargs['input'])['token']=='local-secret'
        return real_run([sys.executable,*shlex.split(argv[-1])[1:]],**kwargs)
    monkeypatch.setattr(auth.subprocess,'run',transport)
    result=auth.sync(platform_cfg)
    assert result['state']=='ready' and remote_path.read_text().strip()=='local-secret'
    auth.sync(platform_cfg)
    remote_path.write_text('different')
    with pytest.raises(common.WorkflowError) as e:auth.sync(platform_cfg)
    assert e.value.code=='PLATFORM_CREDENTIAL_CONFLICT'
    assert 'secret' not in str(e.value)
    assert capsys.readouterr().out==''


def test_completion_only_success_and_uses_frozen_config(platform_cfg, monkeypatch):
    job='a'*32
    path=Path(platform_cfg['local']['state_dir'])/job/'config.yaml'
    common.write(path,platform_cfg)
    seen=[]
    monkeypatch.setattr(auth,'sync',lambda c: seen.append(c['platform']['token_file']) or {'state':'ready'})
    for kind,state in [('init','running'),('init','failed'),('update','success'),('replay','success')]:
        value={'task_type':kind,'state':state}
        assert auth.complete_init(platform_cfg,job,value)==value
    assert not seen
    original=platform_cfg['platform']['token_file']
    platform_cfg['platform']['token_file']='/changed/token'
    assert auth.complete_init(platform_cfg,job,{'task_type':'init','state':'success'})['auth']['state']=='ready'
    assert seen==[original]


def test_wait_completes_init_credentials(cfg, monkeypatch):
    monkeypatch.setattr(jobs,'query_status',lambda *_: {'state':'success','task_type':'init'})
    monkeypatch.setattr(auth,'complete_init',lambda c,j,d: dict(d,auth={'state':'ready'}))
    assert jobs.wait_status(cfg,'a'*32)['auth']['state']=='ready'


@pytest.mark.parametrize('state', ['success','failed','queued'])
def test_cli_init_sync_after_success_only(platform_cfg, monkeypatch, state):
    platform_cfg['python_env'] = {}
    monkeypatch.setattr(config,'config',lambda *_:platform_cfg)
    monkeypatch.setattr(jobs,'local_preflight',lambda *a:None)
    monkeypatch.setattr(sys,'argv',['afxx','env','init','--detach'])
    calls=[]
    monkeypatch.setattr(auth,'prepare',lambda c:calls.append('prepare'))
    monkeypatch.setattr(jobs,'submit',lambda *a: calls.append('submit') or {'job':'a'*32,'state':state})
    monkeypatch.setattr(auth,'complete_init',lambda c,j,d:calls.append('sync') or d)
    cli.main()
    assert calls==['prepare','submit']+(['sync'] if state=='success' else [])


def test_dry_run_never_reads_credentials(platform_cfg, monkeypatch):
    platform_cfg['python_env'] = {}
    monkeypatch.setattr(config,'config',lambda *_:platform_cfg)
    monkeypatch.setattr(jobs,'local_preflight',lambda *a:None)
    monkeypatch.setattr(sys,'argv',['afxx','env','init','--dry-run'])
    monkeypatch.setattr(auth,'prepare',lambda c:pytest.fail('dry run reads credentials'))
    monkeypatch.setattr(jobs,'submit',lambda *a:{'job':'a'*32,'state':'prepared'})
    cli.main()


@pytest.mark.parametrize("value", [None, ""])
def test_empty_token_paths_default_under_user_and_workflow_roots(platform_cfg, monkeypatch, tmp_path, value):
    monkeypatch.setenv("HOME", str(tmp_path))
    platform_cfg['workflow_root']='/mnt/workspace/test-auth'
    platform_cfg['platform'].update(token_file=value, remote_token_file=value, client_token_file=value)
    result=config.normalize_config(platform_cfg)
    assert result['platform']['token_file']==str(tmp_path/'.config/afxx/auth/afx.token')
    assert result['platform']['remote_token_file']==str(Path(platform_cfg['workflow_root'])/'auth/afx.token')
    assert result['platform']['client_token_file']==result['platform']['remote_token_file']


def test_init_response_keeps_auth_summary():
    data=cli.response('init',{'job':'a'*32,'state':'success','auth':{'state':'ready'}})['data']
    assert data['auth']=={'state':'ready'}


def test_no_platform_never_reads_token(cfg, monkeypatch):
    monkeypatch.setattr(auth,'read_token',lambda *a,**k:pytest.fail('no platform'))
    auth.prepare(cfg)


def test_sync_failure_preserves_successful_environment(platform_cfg, monkeypatch):
    job='b'*32
    common.write(Path(platform_cfg['local']['state_dir'])/job/'config.yaml',platform_cfg)
    def failed(c):
        raise common.WorkflowError('PLATFORM_CREDENTIAL_SYNC_FAILED','retry credential transfer')
    monkeypatch.setattr(auth,'sync',failed)
    with pytest.raises(common.WorkflowError) as e:
        auth.complete_init(platform_cfg,job,{'state':'success','task_type':'init'})
    assert e.value.data=={'job_id':job,'environment_state':'success'}


def test_explicit_credential_paths_preserved(platform_cfg):
    platform_cfg['platform']['client_token_file']='/remote/client.token'
    normalized=config.normalize_config(platform_cfg)
    assert normalized['platform']==platform_cfg['platform']


@pytest.mark.parametrize('code', ['PLATFORM_CREDENTIAL_INVALID', 'PLATFORM_UNAUTHORIZED', 'PLATFORM_FORBIDDEN', 'PLATFORM_HTTP_ERROR', 'PLATFORM_UNAVAILABLE'])
def test_init_token_hint_preserves_error_code(platform_cfg, monkeypatch, code):
    def fail(*args, **kwargs):
        raise common.WorkflowError(code, 'original message', {'detail': 'safe'})
    monkeypatch.setattr(auth, 'read_token', lambda *a, **k: 'secret')
    monkeypatch.setattr(auth.platform, 'call', fail)
    with pytest.raises(common.WorkflowError) as caught:
        auth.prepare(platform_cfg)
    assert caught.value.code == code
    assert caught.value.data == {'detail': 'safe'}
    assert str(caught.value).startswith('original message')
    assert ('整个 platform 段' in str(caught.value)) == (code != 'PLATFORM_UNAVAILABLE')
