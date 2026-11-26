import json
import sys
import pytest
from afxx import cli, config, jobs, common
from afxx.afx import platform as p


CASES = [
    (["issue", "detail", "--issue-id", "F152"], "/api/issues/F152", {"issue_id": "F152", "rounds": [], "events": []}),
    (["issue", "tasks", "--issue-id", "F152", "--round-no", "2"], "/api/issues/F152/rounds/2/tasks", [{"task_id": 1, "result": "fail", "detail": "compile error"}]),
    (["issue", "regression-preview", "--issue-id", "F152"], "/api/regression/preview?issue_id=F152", {"issue_id": "F152", "cases": [], "budget": 200}),
    (["case", "execution", "--exec-uuid", "exec-0001"], "/api/executions/exec-0001", {"exec_uuid": "exec-0001", "log": "failure", "log_truncated": True}),
]


@pytest.mark.parametrize("args,path,data", CASES)
def test_queries_are_local_read_only_without_claim(platform_cfg, monkeypatch, capsys, args, path, data):
    monkeypatch.setattr(sys, "argv", ["afxx", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("must not use NPU"))
    monkeypatch.setattr(p, "claimed", lambda *a: pytest.fail("must not require lease"))
    def api(c, method, target, body=None, **kw):
        assert method == "GET" and target == path and body is None
        assert not kw.get("remote")
        return data
    monkeypatch.setattr(p, "call", api)
    assert cli.cli() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == " ".join(args[:2])
    assert result["data"] == (dict(issue_id="F152", round_no=2, tasks=data) if args[1] == "tasks" else data)


@pytest.mark.parametrize("args", [
    ["execution", "show", "--exec-uuid", "exec-0001"],
    ["case", "execution"],
    ["issue", "detail", "--issue-id", "../F152"],
    ["issue", "tasks", "--issue-id", "F152", "--round-no", "0"],
    ["issue", "tasks", "--issue-id", "F152", "--round-no", "1000000"],
    ["issue", "tasks", "--issue-id", "F152"],
    ["case", "execution", "--exec-uuid", "../exec-0001"],
    ["case", "execution", "--exec-uuid", "exec-0001?secret"],
    ["case", "execution", "--exec-uuid", "123"],
])
def test_invalid_query_never_reaches_platform(platform_cfg, monkeypatch, capsys, args):
    monkeypatch.setattr(sys, "argv", ["afxx", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(p, "call", lambda *a, **k: pytest.fail("invalid request"))
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_query_platform_errors_propagate(platform_cfg, monkeypatch, capsys):
    args = ["issue", "detail", "--issue-id", "F152"]
    monkeypatch.setattr(sys, "argv", ["afxx", *args])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    def fail(*a, **kw):
        raise common.WorkflowError("PLATFORM_NOT_FOUND", "not found")
    monkeypatch.setattr(p, "call", fail)
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "PLATFORM_NOT_FOUND"


def test_tasks_empty_and_execution_missing_log_preserved(platform_cfg, monkeypatch):
    monkeypatch.setattr(p, "call", lambda *a, **k: [])
    assert p.issue_tasks(platform_cfg, "F152", 1)["tasks"] == []
    value = {"exec_uuid": "exec-0001", "log_missing": True, "shapes": []}
    monkeypatch.setattr(p, "call", lambda *a, **k: value)
    assert p.execution_detail(platform_cfg, "exec-0001") == value


@pytest.mark.parametrize("query,args,value", [
    (p.issue_detail, ("F152",), {"issue_id": "F153"}),
    (p.issue_tasks, ("F152", 1), {}),
    (p.regression_preview, ("F152",), {"issue_id": "F153"}),
    (p.execution_detail, ("exec-0001",), {"exec_uuid": "exec-0002"}),
])
def test_query_rejects_malformed_response(platform_cfg, monkeypatch, query, args, value):
    monkeypatch.setattr(p, "call", lambda *a, **k: value)
    with pytest.raises(common.WorkflowError) as error:
        query(platform_cfg, *args)
    assert error.value.code == "PLATFORM_RESPONSE_INVALID"


@pytest.mark.parametrize("action,method", [("renew", "renew"), ("status", "poll"), ("wait", "wait")])
def test_issue_control_commands_route_and_return_public_response(platform_cfg, monkeypatch, capsys, action, method):
    args = ["afxx", "issue", action, "--issue-id", "F152"]
    if action != "renew":
        args += ["--round-no", "2"]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("must not use NPU"))
    lease = {"id": "F152", "lease_id": "lease"}
    monkeypatch.setattr(p, "claimed", lambda c, issue: lease)
    calls = []
    def handle(*args):
        calls.append(args)
        return {"issue_id": "F152"}
    monkeypatch.setattr(p, method, handle)
    assert cli.cli() == 0
    response = json.loads(capsys.readouterr().out)
    assert response["command"] == "issue " + action
    assert response["data"] == {"issue_id": "F152"}
    assert len(calls) == 1 and calls[0][0] == platform_cfg
    assert calls[0][1] == (lease if action == "renew" else "F152")
    if action != "renew":
        assert calls[0][2] == 2


@pytest.mark.parametrize("args,path,data", [
    (["case", "spec", "--case-key", "seed"], "/api/cases/seed/spec", {}),
    (["case", "history", "--case-key", "seed", "--after", "42", "--limit", "2"], "/api/cases/seed/history?limit=2&after=42", {"case": {"case_key": "seed"}, "executions": [], "total": 0, "next_cursor": None}),
    (["retest", "list"], "/api/retests", []),
])
def test_new_queries(platform_cfg, monkeypatch, capsys, args, path, data):
    test_queries_are_local_read_only_without_claim(platform_cfg, monkeypatch, capsys, args, path, data)


@pytest.mark.parametrize("args", [
    ["case", "history", "--case-key", "seed", "--limit", "201"],
    ["case", "history", "--case-key", "seed", "--after", "-1"],
    ["case", "spec", "--case-key", ".."],
    ["issue", "release", "--issue-id", "F152", "--request-id", "old"],
])
def test_invalid_new_queries(platform_cfg, monkeypatch, capsys, args):
    test_invalid_query_never_reaches_platform(platform_cfg, monkeypatch, capsys, args)


def test_release_retry_and_reclaim(platform_cfg, monkeypatch):
    path = p.record_path(platform_cfg, "F152")
    lease = dict(id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1")
    common.write(path, lease)
    calls = []
    def api(c, method, target, body):
        assert (method, target) == ("POST", "/api/issues/F152/release")
        calls.append(body.copy())
        if len(calls) == 1:
            raise common.WorkflowError("PLATFORM_UNAVAILABLE", "lost response")
        return dict(issue_id="F152", status="open")
    monkeypatch.setattr(p, "call", api)
    with pytest.raises(common.WorkflowError):
        p.release(platform_cfg, "F152", "release-1")
    assert p.claimed(platform_cfg, "F152") == lease
    expected = dict(issue_id="F152", status="open")
    assert p.release(platform_cfg, "F152", "release-1") == expected
    assert p.release(platform_cfg, "F152", "release-1") == expected
    assert len(calls) == 2 and calls[0] == calls[1]
    with pytest.raises(common.WorkflowError) as e:
        p.claimed(platform_cfg, "F152")
    assert e.value.code == "ISSUE_RELEASED"
    common.write(path, dict(lease, lease_id="new-lease"))
    with pytest.raises(common.WorkflowError) as e:
        p.release(platform_cfg, "F152", "release-1")
    assert e.value.code == "REQUEST_CONFLICT"
    assert p.claimed(platform_cfg, "F152")["lease_id"] == "new-lease"


@pytest.mark.parametrize("query,args", [(p.case_spec, ("seed",)), (p.case_history, ("seed",)), (p.retest_list, ())])
def test_new_query_invalid_response(platform_cfg, monkeypatch, query, args):
    monkeypatch.setattr(p, "call", lambda *a: None)
    with pytest.raises(common.WorkflowError) as e:
        query(platform_cfg, *args)
    assert e.value.code == "PLATFORM_RESPONSE_INVALID"


def test_release_cli_uses_local_lease(platform_cfg, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["afxx", "issue", "release", "--issue-id", "F152", "--request-key", "release-1"])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("must not use NPU"))
    common.write(p.record_path(platform_cfg, "F152"), dict(id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1"))
    def api(c, method, target, body):
        assert body == {"lease_id": "lease", "request_key": "release-1"}
        return {"issue_id": "F152", "status": "open"}
    monkeypatch.setattr(p, "call", api)
    assert cli.cli() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["command"] == "issue release" and value["data"]["status"] == "open"


def test_invalid_release_response_does_not_retire_lease(platform_cfg, monkeypatch):
    lease = dict(id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1")
    common.write(p.record_path(platform_cfg, "F152"), lease)
    monkeypatch.setattr(p, "call", lambda *a: {"issue_id": "F153", "status": "open"})
    with pytest.raises(common.WorkflowError) as e:
        p.release(platform_cfg, "F152", "release-1")
    assert e.value.code == "PLATFORM_RESPONSE_INVALID"
    assert p.claimed(platform_cfg, "F152") == lease


@pytest.mark.parametrize('args,path,data', [
    (['issue','list'], '/api/issues?limit=50', {'issues': [], 'next_cursor': None}),
    (['issue','list','--status','open','--after','1:20:152','--q','a & b'], '/api/issues?limit=50&status=open&after=1%3A20%3A152&q=a+%26+b', {'issues': [], 'next_cursor': '1:10:123'}),
    (['issue','cases','--issue-id','F152'], '/api/issues/F152/cases?role=affected', {'cases': [], 'next_cursor': None}),
    (['issue','cases','--issue-id','F152','--role','regression','--after','42'], '/api/issues/F152/cases?role=regression&after=42', {'cases': [{'case_id':43,'stale':True}], 'next_cursor':43}),
    (['platform','me'], '/api/me', {'user_id':'user','role':'developer'}),
    (['platform','summary'], '/api/ops/summary?scope=mine', {'clients':[], 'users':[]}),
    (['platform','summary','--scope','all'], '/api/ops/summary?scope=all', {'clients':[]}),
])
def test_cli_discovery_queries(platform_cfg, monkeypatch, capsys, args, path, data):
    test_queries_are_local_read_only_without_claim(platform_cfg, monkeypatch, capsys, args, path, data)


@pytest.mark.parametrize('args', [
    ['issue','list','--limit','0'], ['issue','list','--limit','201'],
    ['issue','list','--q',''], ['issue','list','--backend','bad&filter'],
    ['issue','list','--issue-id','F152'],
    ['issue','cases','--issue-id','F152','--after','-1'],
    ['issue','cases','--issue-id','F152','--role','unknown'],
    ['issue','cases','--issue-id','F152','--limit','50'],
    ['platform','summary','--scope','unknown'],
])
def test_cli_discovery_invalid_arguments(platform_cfg, monkeypatch, capsys, args):
    test_invalid_query_never_reaches_platform(platform_cfg, monkeypatch, capsys, args)


@pytest.mark.parametrize('query,args', [(p.issue_list,()), (p.issue_cases,('F152',)), (p.platform_me,()), (p.platform_summary,())])
def test_discovery_response_validation_and_errors(platform_cfg, monkeypatch, query, args):
    monkeypatch.setattr(p, 'call', lambda *a: {})
    with pytest.raises(common.WorkflowError) as error:
        query(platform_cfg, *args)
    assert error.value.code == 'PLATFORM_RESPONSE_INVALID'
    def fail(*a):
        raise common.WorkflowError('PLATFORM_UNAUTHORIZED', 'unauthorized')
    monkeypatch.setattr(p, 'call', fail)
    with pytest.raises(common.WorkflowError) as error:
        query(platform_cfg, *args)
    assert error.value.code == 'PLATFORM_UNAUTHORIZED'


@pytest.mark.parametrize('options,path,data', [
    ([], '/api/cases?limit=50', {'groups':[], 'next_cursor':None}),
    (['--backend','af-mlir','--status','fail','--arch','A2A3','--after','42','--limit','2'], '/api/cases?limit=2&backend=af-mlir&status=fail&arch=A2A3&after=42', {'groups':[{'case':{'case_id':41},'executions':[{'stale_graph':True}], 'more_executions':True}], 'next_cursor':41}),
    (['--q','op:add & dtype:float32'], '/api/cases?limit=50&q=op%3Aadd+%26+dtype%3Afloat32', {'groups':[], 'next_cursor':None}),
    (['--case-key','import/key+1'], '/api/cases?limit=50&case_key=import%2Fkey%2B1', {'groups':[], 'next_cursor':None}),
    (['--case-id','3'], '/api/cases?limit=50&case_id=3', {'groups':[], 'next_cursor':None}),
])
def test_case_list_cli(platform_cfg, monkeypatch, capsys, options, path, data):
    test_queries_are_local_read_only_without_claim(platform_cfg, monkeypatch, capsys, ['case','list',*options], path, data)


@pytest.mark.parametrize('options', [
    ['--limit','201'], ['--after','-1'], ['--case-id','0'], ['--q','  '],
    ['--case-id','1','--case-key','seed'], ['--status','fail'], ['--arch','A2A3'],
    ['--q','add','--backend','af-mlir'], ['--q','add','--arch','A2A3'],
    ['--case-key','seed','--after','10'], ['--case-id','1','--backend','cpu'],
    ['--backend','bad&filter'], ['--status','unknown'],
])
def test_case_list_rejects_ignored_or_invalid_options(platform_cfg, monkeypatch, capsys, options):
    test_invalid_query_never_reaches_platform(platform_cfg, monkeypatch, capsys, ['case','list',*options])


@pytest.mark.parametrize('value', [{}, {'groups': [],'next_cursor':'bad'}, {'groups':[{}],'next_cursor':None}])
def test_case_list_invalid_response(platform_cfg, monkeypatch, value):
    monkeypatch.setattr(p,'call',lambda *a: value)
    with pytest.raises(common.WorkflowError) as error:
        p.case_list(platform_cfg)
    assert error.value.code == 'PLATFORM_RESPONSE_INVALID'


def test_case_list_error_propagates(platform_cfg, monkeypatch):
    def fail(*a):
        raise common.WorkflowError('PLATFORM_UNAVAILABLE','unavailable')
    monkeypatch.setattr(p,'call',fail)
    with pytest.raises(common.WorkflowError) as error:
        p.case_list(platform_cfg)
    assert error.value.code == 'PLATFORM_UNAVAILABLE'


@pytest.mark.parametrize('regression', [False, True])
def test_round_summary_optional_tasks(platform_cfg, monkeypatch, regression):
    verify=dict(round_no=2,kind='verify',fix_version='v',fix_sha256='a'*64,finished_at=1,outcome='passed')
    rounds=[verify]
    if regression:
        rounds.append(dict(verify,round_no=3,kind='regress',finished_at=None,outcome=None))
    detail={'rounds':rounds,'status':'regressing'}
    monkeypatch.setattr(p,'issue_detail',lambda *a: detail)
    calls=[]
    def tasks(c, issue, number):
        calls.append(number)
        return {'tasks':[{'task_id':number,'status':'pending'}]}
    monkeypatch.setattr(p,'issue_tasks',tasks)
    result=p.poll(platform_cfg,'F152',2)
    assert calls==[] and 'tasks' not in result['verification']
    result=p.poll(platform_cfg,'F152',2,include_tasks=True)
    assert calls==([2,3] if regression else [2])
    assert result['verification']['tasks']==[{'task_id':2,'status':'pending'}]
    assert 'tasks' not in verify
    if regression:
        assert result['regression']['tasks'][0]['task_id']==3 and not result['terminal']
    else:
        assert result['regression'] is None and result['terminal']
    def fail(*a): raise common.WorkflowError('PLATFORM_UNAVAILABLE','failed')
    monkeypatch.setattr(p,'issue_tasks',fail)
    with pytest.raises(common.WorkflowError,match='failed'):
        p.poll(platform_cfg,'F152',2,include_tasks=True)


def test_cli_issue_status_include_tasks(platform_cfg, monkeypatch, capsys):
    monkeypatch.setattr(sys,'argv',['afxx','issue','status','--issue-id','F152','--round-no','2','--include-tasks'])
    monkeypatch.setattr(config,'config',lambda _: platform_cfg)
    def poll(c,issue,number,*,include_tasks=False):
        assert issue=='F152' and number==2 and include_tasks
        return {'verification':{'tasks':[]},'regression':None}
    monkeypatch.setattr(p,'poll',poll)
    assert cli.cli()==0
    assert json.loads(capsys.readouterr().out)['data']['verification']['tasks']==[]


def test_claim_id_required(platform_cfg, monkeypatch, capsys):
    test_invalid_query_never_reaches_platform(platform_cfg,monkeypatch,capsys,['issue','claim'])


def test_verify_check_cli_readonly(platform_cfg,monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['afxx','issue','verify-check','--issue-id','F152','--round-no','1','--wait','--timeout','30'])
    monkeypatch.setattr(config,'config',lambda _:platform_cfg)
    monkeypatch.setattr(jobs,'local_preflight',lambda *a:pytest.fail('no NPU'))
    def check(c,issue,n,**kw):
        assert issue=='F152' and n==1 and kw==dict(wait=True,timeout=30)
        return dict(state='running',timed_out=True)
    monkeypatch.setattr(p,'verify_check',check)
    assert cli.cli()==0
    assert json.loads(capsys.readouterr().out)['data']['timed_out']


def test_release_automatic_key_retry_and_new_lease(platform_cfg, monkeypatch):
    path = p.record_path(platform_cfg, "F152")
    lease = dict(id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1")
    common.write(path, lease)
    bodies = []
    def api(c, method, target, body):
        bodies.append(body.copy())
        if len(bodies) == 1:
            raise common.WorkflowError("PLATFORM_UNAVAILABLE", "lost response")
        return dict(issue_id="F152", status="open")
    monkeypatch.setattr(p, "call", api)
    with pytest.raises(common.WorkflowError):
        p.release(platform_cfg, "F152")
    p.release(platform_cfg, "F152")
    p.release(platform_cfg, "F152")
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert bodies[0]["request_key"].startswith("release-")
    common.write(path, dict(lease, lease_id="new-lease"))
    p.release(platform_cfg, "F152")
    assert bodies[-1]["request_key"] != bodies[0]["request_key"]


@pytest.mark.parametrize('key', ['', 'bad/key', 'x'*129, 123])
def test_mutation_key_validation(key):
    with pytest.raises(common.WorkflowError):
        p.mutation_request_key('release', 'F152', {}, {}, key)


def test_mutation_key_identity():
    key = p.mutation_request_key('release', 'F152', {'platform_url':'a'}, {'lease_id':'l'})
    assert key != p.mutation_request_key('summary', 'F152', {'platform_url':'a'}, {'lease_id':'l'})
    assert key != p.mutation_request_key('release', 'F152', {'platform_url':'b'}, {'lease_id':'l'})
    assert p.mutation_request_key('release', 'F152', {}, {}, 'manual') == 'manual'


def test_release_cli_default_key(platform_cfg, monkeypatch, capsys):
    common.write(p.record_path(platform_cfg, "F152"), dict(id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1"))
    monkeypatch.setattr(sys, "argv", ["afxx", "issue", "release", "--issue-id", "F152"])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    def api(c, method, target, body):
        assert body["request_key"].startswith("release-")
        return dict(issue_id="F152", status="open")
    monkeypatch.setattr(p, "call", api)
    assert cli.cli() == 0
    assert json.loads(capsys.readouterr().out)["ok"]
