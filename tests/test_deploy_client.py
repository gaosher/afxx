"""Installation boundaries and restoration semantics for opt-in deploy coordination."""
from pathlib import Path
import pytest
from afxx import common, deploy_client as d


@pytest.fixture
def setup(cfg, tmp_path, monkeypatch):
    job = tmp_path/'deploy'
    job.mkdir()
    home = tmp_path/'client'
    home.mkdir()
    common.write(job/'request.json', {'coordinate_client': True})
    manifest = {'python':'/execution/bin/python', 'env':{'ASCEND_HOME_PATH':str(Path(cfg['runtime']['install_root'])/'cann')}}
    state = {'state':'running', 'daemon_running':True, 'home':str(home), 'runs':[],
             'execution_environment':manifest, 'active_runs':[], 'active_operation':None}
    calls=[]
    def control(c, action, **kwargs):
        calls.append((action, kwargs))
        if action == 'stop':
            return {'state':'stopped', 'daemon_running':False}
        return dict(state)
    monkeypatch.setattr(d.launcher,'control',control)
    evidence={k:{'path':'/installed/'+k,'sha256':k} for k in ('pyautofuse','adapter','codegen')}
    def probe(c, env, job, *, interpreter):
        assert interpreter == manifest['python']
        assert env['ASCEND_HOME_PATH'] == manifest['env']['ASCEND_HOME_PATH']
        return evidence
    monkeypatch.setattr(d.runtime,'probe',probe)
    return job, state, calls, evidence


@pytest.mark.parametrize('initial', ['running','idle','stopped'])
def test_success_restores_only_original_intent(cfg, setup, initial):
    job,state,calls,evidence=setup
    state.update(state=initial,daemon_running=initial!='stopped')
    with d.coordinate(cfg,job) as coordination:
        assert [a for a,_ in calls].count('stop') == (initial!='stopped')
        coordination.install_started=True
        common.update_record(job/'result.json',state='success',runtime=evidence)
        coordination.installed=True
    record=common.read(job/'result.json')['client_coordination']
    if initial=='stopped':
        assert record['state']=='unchanged'
        assert all(action=='status' for action,_ in calls)
    else:
        assert record['state']=='restored'
        start=next(kwargs for action,kwargs in calls if action=='start')
        assert start['daemon_only'] == (initial=='idle')
        assert start['home'] == state['home']


@pytest.mark.parametrize('initial', ['paused','preparing','draining','needs_attention'])
def test_unsupported_states_never_stop_or_install(cfg,setup,initial):
    job,state,calls,_=setup
    state['state']=initial
    with pytest.raises(common.WorkflowError,match='paused, transitioning'):
        with d.coordinate(cfg,job):
            pytest.fail('must not install')
    assert all(action=='status' for action,_ in calls)


@pytest.mark.parametrize('installed', [False,True])
def test_failure_before_install_restores_but_partial_install_stays_stopped(cfg,setup,installed):
    job,state,calls,_=setup
    with pytest.raises(ValueError,match='failure'):
        with d.coordinate(cfg,job) as coordination:
            coordination.install_started=installed
            raise ValueError('failure')
    assert any(action=='start' for action,_ in calls) == (not installed)
    assert common.read(job/'result.json')['client_coordination']['state'] == ('held_stopped' if installed else 'restored')


def test_environment_mismatch_and_active_operation_leave_client_untouched(cfg,setup):
    job,state,calls,_=setup
    state['execution_environment']['env']['ASCEND_HOME_PATH']='/other/cann'
    with pytest.raises(common.WorkflowError,match='CANN differs'):
        with d.coordinate(cfg,job): pytest.fail('must not install')
    state['active_operation']='upgrading'
    with pytest.raises(common.WorkflowError,match='active operation'):
        with d.coordinate(cfg,job): pytest.fail('must not install')
    assert all(action=='status' for action,_ in calls)


def test_restore_failure_preserves_successful_install(cfg,setup,monkeypatch):
    job,state,calls,evidence=setup
    original=d.launcher.control
    def fail(c,action,**kwargs):
        if action=='start': raise ValueError('cannot start')
        return original(c,action,**kwargs)
    monkeypatch.setattr(d.launcher,'control',fail)
    with pytest.raises(common.WorkflowError) as error:
        with d.coordinate(cfg,job) as coordination:
            coordination.install_started=True
            common.update_record(job/'result.json',state='success',runtime=evidence)
            coordination.installed=True
    assert error.value.code=='DEPLOY_CLIENT_RESTORE_FAILED'
    record=common.read(job/'result.json')
    assert record['state']=='success' and record['client_coordination']['state']=='restore_failed'


def test_disabled_does_not_touch_client(cfg,setup,monkeypatch):
    job,_,_,_=setup
    common.write(job/'request.json',{})
    monkeypatch.setattr(d.launcher,'control',lambda *a,**k:pytest.fail('not requested'))
    with d.coordinate(cfg,job) as coordination: assert coordination is None


@pytest.mark.parametrize('restore_fails', [False, True])
def test_deploy_orders_build_stop_install_restore_and_keeps_reference_valid(cfg, tmp_path, monkeypatch, restore_fails):
    from afxx import execution
    job=tmp_path/'job'
    job.mkdir()
    source=Path(cfg['remote']['source_dir'])
    source.mkdir(parents=True)
    home=tmp_path/'home';home.mkdir()
    root=Path(cfg['runtime']['install_root']);root.mkdir(parents=True)
    common.write(job/'request.json',{'coordinate_client':True})
    common.write(job/'source.json',{})
    events=[]
    evidence={}
    monkeypatch.setattr(execution,'record_environment',lambda *a:None)
    monkeypatch.setattr(d.runtime,'environment',lambda c:{})
    monkeypatch.setattr(d.runtime,'venv_preflight',lambda *a:None)
    monkeypatch.setattr(d.runtime,'registry',lambda c:tmp_path/'registry')
    monkeypatch.setattr(d.runtime,'packaged_libraries',lambda *a:{'pyautofuse.so'})
    monkeypatch.setattr(d.runtime,'check_idle',lambda *a,**k:events.append('idle-check'))
    monkeypatch.setattr(execution.workflow_source,'sync_source',lambda *a:(source,{'sha256':'snapshot','base_commit':'base'}))
    manifest={'python':'/execution/bin/python','env':{'ASCEND_HOME_PATH':str(root/'cann')}}
    state={'state':'running','daemon_running':True,'home':str(home),'execution_environment':manifest,
           'active_runs':[],'active_operation':None}
    def control(c,action,**kwargs):
        if action=='stop':
            events.append('stop');return {'state':'stopped','daemon_running':False}
        if action=='start':
            events.append('restore')
            if restore_fails:raise ValueError('restore failed')
        return state
    monkeypatch.setattr(d.launcher,'control',control)
    def run(argv,**kwargs):
        if 'build.sh' in argv:
            events.append('build')
            (job/'packages/cann-graph-autofusion_test.run').write_text('package')
            for key in ('pyautofuse','adapter','codegen'):
                file=source/'build'/key;file.write_text(key)
                installed=root/key;installed.write_text(key)
                evidence[key]={'path':str(installed),'sha256':common.sha(installed)}
        elif '--check' in argv: events.append('package-check')
        elif '--upgrade' in argv: events.append('install')
    monkeypatch.setattr(common,'run',run)
    monkeypatch.setattr(d.runtime,'probe',lambda *a,**k:evidence)
    if restore_fails:
        with pytest.raises(common.WorkflowError,match='restoration failed'):execution.deploy(cfg,job)
    else:execution.deploy(cfg,job)
    assert events[:6]==['build','package-check','stop','idle-check','install','restore']
    assert common.read(tmp_path/'registry.json')==d.runtime.deployment_reference(job/'result.json')
    report=common.read(job/'result.json')
    assert report['state']=='success'
    assert report['client_coordination']['state']==('restore_failed' if restore_fails else 'restored')
    if restore_fails:
        with pytest.raises(common.WorkflowError,match='coordination did not complete'):
            d.runtime.current_deployment(cfg,job/'result.json')


def test_cli_freezes_opt_in_and_mcp_forwards_it(cfg, monkeypatch):
    import sys
    from afxx import cli, jobs
    monkeypatch.setattr(cli.workflow_config,'config',lambda _:cfg)
    submitted=[]
    monkeypatch.setattr(jobs,'submit',lambda c,r,k,d:submitted.append(r) or {'job':'dry','dry_run':True})
    monkeypatch.setattr(sys,'argv',['afxx','deploy','--coordinate-client','--dry-run'])
    cli.main()
    assert submitted[0]['coordinate_client'] is True
    pytest.importorskip('mcp')
    from afxx import mcp_server, mcp_operations
    from test_mcp_tools import call
    calls=[]
    monkeypatch.setattr(mcp_operations,'submit',lambda command,args,path,key:calls.append(args) or {'ok':True, 'data':{}})
    call(mcp_server.create_server('/config'), 'afxx_deploy', {'request_id':'deploy-1','coordinate_client':True})
    assert '--coordinate-client' in calls[0]
