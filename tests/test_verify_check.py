import copy
import pytest
from afxx.afx import platform as p
from afxx import common


def setup(monkeypatch, status='verifying', outcome=None, regression=False, result='pending', newer=False):
    v=dict(round_no=1,kind='verify',fix_version='v',fix_sha256='a'*64,finished_at=1 if outcome else None,outcome=outcome)
    rounds=[v]
    if regression: rounds.append(dict(v,round_no=2,kind='regress',finished_at=None,outcome=None))
    if newer: rounds.append(dict(v,round_no=3))
    detail={'rounds':rounds,'status':status}
    monkeypatch.setattr(p,'issue_detail',lambda *a: copy.deepcopy(detail))
    def tasks(c,issue,n):
        r='pass' if regression and n==1 else result
        return {'tasks':[{'task_id':n,'case_key':'seed','result':r,'status':'done' if r=='pass' else r,'detail':'error'}]}
    monkeypatch.setattr(p,'issue_tasks',tasks)
    monkeypatch.setattr(p,'renew',lambda *a,**k: pytest.fail('must not renew'))
    monkeypatch.setattr(p,'claimed',lambda *a,**k: pytest.fail('must not require lease'))
    return detail


@pytest.mark.parametrize('status,newer,state', [('verifying',False,'running'),('regressing',False,'running'),('summarizing',False,'passed'),('resolved',False,'passed'),('summarizing',True,'attention'),('open',False,'attention')])
def test_no_regression_requires_current_lifecycle(platform_cfg,monkeypatch,status,newer,state):
    setup(monkeypatch,status,'passed',result='pass',newer=newer)
    r=p.verify_check(platform_cfg,'F152',1)
    assert r['state']==state
    assert r['reason']==('no_regression_candidates' if state=='passed' else None if state=='running' else 'cannot_confirm_regression_selection')


@pytest.mark.parametrize('outcome,result,state', [('failed','fail','failed'),('invalid','invalid','invalid'),('passed','skip','attention'),('passed','cancelled','attention'),(None,'pending','running')])
def test_results_not_misclassified(platform_cfg,monkeypatch,outcome,result,state):
    setup(monkeypatch,'verifying',outcome,result=result)
    r=p.verify_check(platform_cfg,'F152',1)
    assert r['state']==state
    assert 'exec_uuid' not in (r['failures'][0] if r['failures'] else {})


def test_regression_and_identity(platform_cfg,monkeypatch):
    d=setup(monkeypatch,'regressing','passed',True)
    assert p.verify_check(platform_cfg,'F152',1)['phase']=='regression'
    d['rounds'][1]['fix_sha256']='b'*64
    with pytest.raises(ValueError,match='candidate differs'):
        p.verify_check(platform_cfg,'F152',1)


def test_wait_timeout_and_transition(platform_cfg,monkeypatch):
    d=setup(monkeypatch)
    clock=[0.0]
    monkeypatch.setattr(p.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(p.time,'sleep',lambda n:clock.__setitem__(0,clock[0]+n))
    r=p.verify_check(platform_cfg,'F152',1,wait=True,timeout=1)
    assert r['timed_out'] and r['state']=='running' and r['next_action']=='wait'
    def finish(n):
        clock[0]+=n
        d['rounds'][0].update(outcome='failed',finished_at=1)
    monkeypatch.setattr(p.time,'sleep',finish)
    assert p.verify_check(platform_cfg,'F152',1,wait=True,timeout=10)['state']=='failed'


def test_failures_bounded_and_errors_propagate(platform_cfg,monkeypatch):
    setup(monkeypatch,'claimed','failed',result='fail')
    monkeypatch.setattr(p,'issue_tasks',lambda *a: {'tasks':[{'task_id':n,'result':'fail','detail':'x'*2000} for n in range(25)]})
    r=p.verify_check(platform_cfg,'F152',1)
    assert r['failure_count']==25 and r['failures_truncated'] and len(r['failures'])==20
    assert len(r['failures'][0]['detail'])==1000
    def fail(*a): raise common.WorkflowError('PLATFORM_UNAVAILABLE','offline')
    monkeypatch.setattr(p,'issue_tasks',fail)
    with pytest.raises(common.WorkflowError,match='offline'): p.verify_check(platform_cfg,'F152',1)


@pytest.mark.parametrize('options',[{'timeout':1},{'wait':True,'timeout':float('nan')},{'wait':True,'timeout':51}])
def test_wait_invalid(platform_cfg,options):
    with pytest.raises(ValueError): p.verify_check(platform_cfg,'F152',1,**options)


def test_regression_passes_and_empty_tasks_attention(platform_cfg,monkeypatch):
    d=setup(monkeypatch,'summarizing','passed',True,result='pass')
    d['rounds'][1].update(outcome='passed',finished_at=1)
    r=p.verify_check(platform_cfg,'F152',1)
    assert r['state']=='passed' and r['phase']=='regression'
    monkeypatch.setattr(p,'issue_tasks',lambda *a: {'tasks':[]})
    assert p.verify_check(platform_cfg,'F152',1)['state']=='attention'
