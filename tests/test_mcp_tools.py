import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytest.importorskip('mcp')
from afxx import mcp_server as m
from afxx import mcp_operations as op


def call(server, tool, arguments):
    # Exercise FastMCP's real argument validation and invocation path.
    return asyncio.run(server._tool_manager.call_tool(tool, arguments))


@pytest.mark.parametrize('args,explicit', [(['--config','/a'],'/b'), (['--config=/a','--config=/b'],None), (['--config'],None)])
def test_config_conflict_before_execution(monkeypatch, args, explicit):
    async def forbidden(*a): pytest.fail('CLI must not execute')
    monkeypatch.setattr(m,'invoke',forbidden)
    result=call(m.create_server('/default'),'afxx_run',dict(command='client status',arguments=args,config=explicit))
    assert result.structuredContent['error']['code']=='CONFIG_CONFLICT'


def test_config_precedence_and_same_path_dedup(tmp_path):
    p=str(tmp_path/'cfg')
    args,path=m.prepare('status',['--config='+p,'--config',p],p,'/default')
    assert args==['--config',p] and path==p
    assert m.prepare('status',[],None,'/default')[1]=='/default'
    assert m.prepare('config init',[],None,'/default')==([],None)


def test_typed_preview_uses_cli_tokens(monkeypatch):
    seen=[]
    async def invoke(args):
        seen.append(args)
        return 0,json.dumps({'ok':True,'data':{'seeds':['seed']},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    result=call(m.create_server('/config'),'afxx_issue',dict(action='preview',issue_id='F152',full=True))
    assert seen==[['issue','preview','--config','/config','--issue-id','F152','--full']]
    assert result.structuredContent['data']['seeds']==['seed']


def test_typed_replay_and_generic_have_same_invocation(monkeypatch):
    seen=[]
    async def invoke(args):
        seen.append(args)
        return 0, json.dumps({'ok': True, 'data': {'request_id': 'r', 'run': len(seen)}, 'error': None})
    monkeypatch.setattr(m, 'invoke', invoke)
    monkeypatch.setattr(op, 'submit', lambda *a: pytest.fail('replay must not reuse MCP receipt'))
    server=m.create_server('/config')
    call(server,'afxx_replay',dict(request_id='r',seed='ABC',dump=True))
    call(server,'afxx_run',dict(command='replay',request_id='r',arguments=['--seed','ABC','--dump']))
    assert seen[0]==seen[1]
    assert seen[0][-2:]==['--request-id','r']


def test_mutually_exclusive_replay_fields(monkeypatch):
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('must not submit'))
    result=call(m.create_server(),'afxx_replay',dict(request_id='r',seed='ABC',seeds_file='/tmp/seeds'))
    assert result.structuredContent['error']['code']=='INVALID_ARGUMENT'


def test_request_key_conflict(monkeypatch):
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('must not submit'))
    result=call(m.create_server(),'afxx_run',dict(command='deploy',request_id='one',arguments=['--request-id=two']))
    assert result.structuredContent['error']['code']=='REQUEST_CONFLICT'


def test_typed_restart_never_submits_verify(monkeypatch):
    seen=[]
    monkeypatch.setattr(op,'submit',lambda *a: seen.append(a) or {'ok':True,'data':{},'error':None})
    call(m.create_server('/config'),'afxx_client',dict(action='restart',request_id='restart1',timeout=10))
    assert seen[0][0]=='client restart'
    assert seen[0][1]==['client','restart','--config','/config','--timeout','10.0']


def test_schema_has_types_and_constraints():
    tools={t.name:t for t in asyncio.run(m.create_server().list_tools())}
    schema=tools['afxx_replay'].inputSchema
    assert set(schema['required'])=={'request_id'}
    assert schema['properties']['dump']['type']=='boolean'
    assert 'deploy_job_id' not in schema['properties']
    assert 'afxx_operation_status' in tools


def test_long_command_requires_recovery_key():
    result=call(m.create_server(),'afxx_run',dict(command='client restart'))
    assert result.structuredContent['error']['code']=='INVALID_ARGUMENT'


def test_durable_receipt_survives_server_and_preserves_cli_result(tmp_path,monkeypatch):
    monkeypatch.setattr(op,'root',lambda:tmp_path/'receipts')
    # Run the real CLI without contacting the NPU, through a detached worker.
    result=op.submit('help',['help'],None,'durable')
    # help isn't JSON: the worker must return an explicit protocol failure.
    for _ in range(100):
        result=op.status('durable')
        if result['data']['state']=='completed': break
        time.sleep(.05)
    assert result['data']['state']=='completed'
    assert result['data']['result']['error']['code']=='CLI_PROTOCOL_ERROR'
    again=op.submit('help',['help'],None,'durable')
    assert again['data']['operation_id']==result['data']['operation_id']
    assert op.submit('other',['help'],None,'durable')['error']['code']=='REQUEST_CONFLICT'
    # Fresh process can query the same durable receipt; no server task registry is needed.
    code="from pathlib import Path; from afxx import mcp_operations as o; import json; o.root=lambda:Path("+repr(str(tmp_path/'receipts'))+"); print(json.dumps(o.status('durable')))"
    fresh=json.loads(subprocess.check_output([sys.executable,'-c',code],text=True))
    assert fresh['data']['result']==result['data']['result']


def test_unknown_worker_not_resubmitted(tmp_path,monkeypatch):
    monkeypatch.setattr(op,'root',lambda:tmp_path)
    import hashlib
    d=tmp_path/hashlib.sha256(b'missing').hexdigest();d.mkdir()
    request=dict(command='client restart',config=None,submitted_at=0,worker_pid=99999999,
                 input=dict(command='client restart',arguments=['client','restart'],config=None,config_sha256=None,cwd=str(Path.cwd())))
    op.write(d/'request.json',request)
    monkeypatch.setattr(op.subprocess,'Popen',lambda *a,**k: pytest.fail('must not rerun unknown operation'))
    # ps also uses Popen, so provide a safe non-live observation separately.
    monkeypatch.setattr(op.subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a,1,'',''))
    assert op.submit('client restart',['client','restart'],None,'missing')['data']['state']=='unknown'


def test_configuration_change_before_launch(tmp_path):
    config=tmp_path/'config.yaml';config.write_text('changed')
    op.write(tmp_path/'request.json',dict(input=dict(config=str(config),config_sha256='old',arguments=['help'],cwd=str(Path.cwd()))))
    op.worker(tmp_path)
    assert json.loads((tmp_path/'result.json').read_text())['error']['code']=='CONFIG_CHANGED'


def test_real_cli_result_retained(tmp_path,monkeypatch):
    monkeypatch.setattr(op,'root',lambda:tmp_path/'receipts')
    args=['config','init','--source',str(tmp_path),'--output',str(tmp_path/'config.yaml')]
    op.submit('config init',args,None,'success')
    for _ in range(100):
        result=op.status('success')
        if result['data']['state']=='completed': break
        time.sleep(.05)
    assert result['data']['result']['ok'] is True
    assert result['data']['result']['data']['config']==str(tmp_path/'config.yaml')
    assert op.status('absent')['error']['code']=='OPERATION_NOT_FOUND'


def test_stdio_structured_tool_and_operation_reconnect(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import os
    env=dict(os.environ,HOME=str(tmp_path))
    parameters=StdioServerParameters(command=sys.executable,args=['-m','afxx.cli','mcp'],env=env)
    async def scenario():
        async with stdio_client(parameters) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools=await session.list_tools()
                assert any(t.name=='afxx_client' for t in tools.tools)
                # Invalid config fails in the real CLI without ever connecting to NPU.
                submitted=await session.call_tool('afxx_deploy',{'request_id':'stdio-deploy','config':str(tmp_path/'absent')})
                assert submitted.structuredContent['data']['group_request_id']=='stdio-deploy'
                operation_id = submitted.structuredContent['data']['request_id']
        # Closing the first MCP session must not lose or cancel the detached invocation.
        async with stdio_client(parameters) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                for _ in range(100):
                    result=await session.call_tool('afxx_operation_status',{'request_id':operation_id})
                    data=result.structuredContent['data']
                    if data['state']=='completed': break
                    await asyncio.sleep(.05)
                assert data['state']=='completed'
                assert data['result']['error']['code']=='CONFIG_INVALID'
    asyncio.run(scenario())


def test_detached_worker_outlives_submitter(tmp_path):
    # A disposable CLI delays its response so the submitting process exits first.
    package=tmp_path/'afxx';package.mkdir()
    (package/'__init__.py').write_text('')
    (package/'cli.py').write_text("import time,json; time.sleep(0.5); print(json.dumps({'ok':True,'data':{'job_id':'recorded-job'},'error':None}))")
    import os
    env=dict(os.environ,PYTHONPATH=str(tmp_path))
    code="""import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('operations',sys.argv[1]);o=importlib.util.module_from_spec(spec);spec.loader.exec_module(o)
o.root=lambda:Path(sys.argv[2])
print(o.submit('deploy',['deploy'],None,'outlive')['data']['state'],flush=True)
"""
    receipts=tmp_path/'receipts'
    output=subprocess.check_output([sys.executable,'-c',code,str(Path(op.__file__).resolve()),str(receipts)],cwd=tmp_path,env=env,text=True)
    assert output.strip()=='pending'
    import hashlib
    result=receipts/hashlib.sha256(b'outlive').hexdigest()/'result.json'
    for _ in range(100):
        if result.exists():break
        time.sleep(.05)
    assert json.loads(result.read_text())['data']['job_id']=='recorded-job'


@pytest.mark.parametrize('tool,arguments,tokens', [
    ('afxx_issue', {'action':'release','issue_id':'F152','request_key':'release-1'}, ['issue','release','--config','/config','--issue-id','F152','--request-key','release-1']),
    ('afxx_case', {'action':'spec','case_key':'seed'}, ['case','spec','--config','/config','--case-key','seed']),
    ('afxx_case', {'action':'history','case_key':'seed','after':42,'limit':2}, ['case','history','--config','/config','--case-key','seed','--limit','2','--after','42']),
    ('afxx_evidence_list', {'request_id':'repair','run':2}, ['evidence','list','--config','/config','--request-id','repair','--offset','0','--run','2']),
    ('afxx_evidence_read', {'request_id':'repair','run':2,'path':'/remote/a'}, ['evidence','read','--config','/config','--request-id','repair','--path','/remote/a','--offset','0','--limit','8192','--run','2']),
])
def test_platform_tools_share_cli(monkeypatch, tool, arguments, tokens):
    async def invoke(args):
        assert args == tokens
        return 0, json.dumps({'ok':True,'data':[], 'error':None})
    monkeypatch.setattr(m, 'invoke', invoke)
    assert not call(m.create_server('/config'), tool, arguments).isError


def test_case_list_generic_mcp(monkeypatch):
    async def invoke(args):
        assert args == ['case','list','--config','/config','--backend','af-mlir','--status','fail']
        return 0,json.dumps({'ok':True,'data':{'groups':[], 'next_cursor':None},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    result=call(m.create_server('/config'),'afxx_run',{'command':'case list','arguments':['--backend','af-mlir','--status','fail']})
    assert result.structuredContent['data']['groups'] == []
    names={tool.name for tool in asyncio.run(m.create_server().list_tools())}
    assert 'afxx_case_list' not in names


@pytest.mark.parametrize('tool,arguments,tokens', [
    ('afxx_case_execution', {'exec_uuid':'exec-0001'}, ['case','execution','--exec-uuid','exec-0001']),
    ('afxx_issue_status', {'issue_id':'F152','round_no':2,'include_tasks':True}, ['issue','status','--issue-id','F152','--round-no','2','--include-tasks']),
    ('afxx_evidence_list', {'request_id':'a'*32,'offset':10}, ['evidence','list','--request-id','a'*32,'--offset','10']),
    ('afxx_evidence_read', {'request_id':'a'*32,'path':'/remote/dump/a b.mlir'}, ['evidence','read','--request-id','a'*32,'--path','/remote/dump/a b.mlir','--offset','0','--limit','8192']),
    ('afxx_evidence_read', {'request_id':'a'*32,'path':'/remote/a','offset':8192,'limit':1024,'sha256':'b'*64}, ['evidence','read','--request-id','a'*32,'--path','/remote/a','--offset','8192','--limit','1024','--sha256','b'*64]),
])
def test_diagnostic_tools_share_cli(monkeypatch, tool, arguments, tokens):
    payload={'ok':False,'data':None,'error':{'code':'EVIDENCE_UNAVAILABLE','message':'missing'}}
    async def invoke(args):
        assert args == tokens[:2]+['--config','/explicit']+tokens[2:]
        return 2,json.dumps(payload)
    monkeypatch.setattr(m,'invoke',invoke)
    result=call(m.create_server('/default'),tool,dict(arguments,config='/explicit'))
    assert result.isError and result.structuredContent == payload


@pytest.mark.parametrize('tool,args', [
    ('afxx_case_execution', {'exec_uuid':'123'}),
    ('afxx_issue_status', {'issue_id':'F152','round_no':0}),
    ('afxx_issue_status', {'issue_id':'../F152','round_no':1}),
    ('afxx_evidence_list', {'request_id':'../bad'}),
    ('afxx_evidence_list', {'request_id':'a'*32,'offset':-1}),
    ('afxx_evidence_read', {'request_id':'a'*32,'path':'/remote/a','limit':65537}),
    ('afxx_evidence_read', {'request_id':'a'*32,'path':'/remote/a','sha256':'bad'}),
    ('afxx_evidence_list', {'request_id':'repair','run':0}),
    ('afxx_evidence_read', {'request_id':'repair','run':-1,'path':'/remote/a'}),
    ('afxx_evidence_list', {'job_id':'a'*32}),
])
def test_diagnostic_tool_schema_rejects_invalid_args(monkeypatch, tool, args):
    from mcp.server.fastmcp.exceptions import ToolError
    async def forbidden(*a): pytest.fail('must not execute CLI')
    monkeypatch.setattr(m,'invoke',forbidden)
    with pytest.raises(ToolError):
        call(m.create_server(),tool,args)


@pytest.mark.parametrize('path', ['relative/file', '/remote/../secret'])
def test_evidence_read_rejects_invalid_remote_path(monkeypatch,path):
    async def forbidden(*a): pytest.fail('must not execute CLI')
    monkeypatch.setattr(m,'invoke',forbidden)
    result=call(m.create_server(),'afxx_evidence_read',{'request_id':'a'*32,'path':path})
    assert result.structuredContent['error']['code']=='INVALID_ARGUMENT'


@pytest.mark.parametrize('action', ['start','stop','restart'])
def test_client_actions_require_request_id(monkeypatch,action):
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('missing key must not submit'))
    result=call(m.create_server(),'afxx_client',{'action':action})
    assert result.structuredContent['error']['code']=='INVALID_ARGUMENT'


@pytest.mark.parametrize('action', ['start','stop','restart'])
def test_client_actions_dispatch_same_lifecycle(monkeypatch,action):
    seen=[]
    monkeypatch.setattr(op,'submit',lambda *a: seen.append(a) or {'ok':True,'data':{},'error':None})
    call(m.create_server('/config'),'afxx_client',{'action':action,'request_id':'client-1'})
    assert seen[0][0]=='client '+action
    assert seen[0][1]==['client',action,'--config','/config','--timeout','300']


def test_client_status_direct_and_no_request_key(monkeypatch):
    async def invoke(args):
        assert args==['client','status','--config','/config','--timeout','30']
        return 0,json.dumps({'ok':True,'data':{'state':'running'},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('status must not submit'))
    server=m.create_server('/config')
    assert call(server,'afxx_client',{'action':'status'}).structuredContent['data']['state']=='running'
    assert call(server,'afxx_client',{'action':'status','request_id':'unused'}).isError
    names={t.name for t in asyncio.run(server.list_tools())}
    assert 'afxx_client' in names
    assert not names.intersection({'afxx_client_'+a for a in ('status','start','stop','restart')})


@pytest.mark.parametrize('action', ['claim','preview','verify','release'])
def test_issue_requires_explicit_identity(monkeypatch,action):
    from mcp.server.fastmcp.exceptions import ToolError
    async def forbidden(*a): pytest.fail('must not execute')
    monkeypatch.setattr(m,'invoke',forbidden)
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('must not submit'))
    with pytest.raises(ToolError):
        call(m.create_server(),'afxx_issue',{'action':action,'request_id':'r'})


@pytest.mark.parametrize('args', [
    {'action':'claim'},
    {'action':'verify','deploy_job_id':'a'*32},
    {'action':'preview','request_id':'r'}, {'action':'preview','deploy_job_id':'a'*32},
    {'action':'claim','request_id':'r','full':False},
    {'action':'release','request_id':'r','deploy_job_id':'a'*32},
    {'action':'verify','request_id':'r','deploy_job_id':'a'*32,'full':False},
])
def test_issue_action_parameter_contract(monkeypatch,args):
    async def forbidden(*a): pytest.fail('must not execute')
    monkeypatch.setattr(m,'invoke',forbidden)
    monkeypatch.setattr(op,'submit',lambda *a: pytest.fail('must not submit'))
    result=call(m.create_server(),'afxx_issue',dict(args,issue_id='F152'))
    assert result.structuredContent['error']['code']=='INVALID_ARGUMENT'


def test_issue_claim_explicit_and_verify_async(monkeypatch):
    async def invoke(args):
        assert args==['issue','claim','--config','/config','--issue-id','F152','--request-id','r']
        return 0,json.dumps({'ok':True,'data':{'issue_id':'F152'},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    server=m.create_server('/config')
    assert not call(server,'afxx_issue',{'action':'claim','issue_id':'F152','request_id':'r'}).isError
    seen=[]
    monkeypatch.setattr(op,'submit',lambda *a: seen.append(a) or {'ok':True,'data':{},'error':None})
    call(server,'afxx_issue',{'action':'verify','issue_id':'F152','deploy_job_id':'a'*32,'request_id':'v'})
    assert seen[0]==('issue verify',['issue','verify','--config','/config','--issue-id','F152','--job-id','a'*32],'/config','v')
    names={t.name for t in asyncio.run(server.list_tools())}
    assert 'afxx_issue' in names
    assert not names.intersection({'afxx_issue_'+a for a in ('claim','preview','verify','release')})


@pytest.mark.parametrize('args', [
    {'action':'spec','case_key':'seed','limit':50},
    {'action':'spec','case_key':'seed','after':0},
])
def test_case_spec_rejects_pagination(monkeypatch,args):
    async def forbidden(*a): pytest.fail('must not invoke')
    monkeypatch.setattr(m,'invoke',forbidden)
    assert call(m.create_server(),'afxx_case',args).structuredContent['error']['code']=='INVALID_ARGUMENT'


@pytest.mark.parametrize('args', [
    {'fields':['state']}, {'attempt':'a','wait':True}, {'timeout':30},
    {'since':'cursor'}, {'attempt':''}, {'attempt':'a','fields':[]},
    {'attempt':'a','fields':['x,y']},
])
def test_job_status_rejects_ignored_options(monkeypatch,args):
    async def forbidden(*a): pytest.fail('must not invoke')
    monkeypatch.setattr(m,'invoke',forbidden)
    assert call(m.create_server(),'afxx_job_status',dict(args,request_id='a'*32)).isError


@pytest.mark.parametrize('args,tokens', [
    ({'attempt':'attempt-1','fields':['state','result.correctness']}, ['--attempt','attempt-1','--fields','state,result.correctness']),
    ({'wait':True,'since':'cursor'}, ['--wait','--timeout','30','--since','cursor']),
])
def test_job_status_extended_queries(monkeypatch,args,tokens):
    async def invoke(argv):
        assert argv==['status','--config','/config','--request-id','a'*32]+tokens
        return 0,json.dumps({'ok':True,'data':{},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    assert not call(m.create_server('/config'),'afxx_job_status',dict(args,request_id='a'*32)).isError


def test_consolidated_tool_inventory():
    names={t.name for t in asyncio.run(m.create_server().list_tools())}
    assert 'afxx_case' in names
    assert not names.intersection({'afxx_case_spec','afxx_case_history','afxx_retest_list','afxx_issue_tasks'})
    assert len(names)==13


def test_verify_check_mcp_is_direct_readonly(monkeypatch):
    async def invoke(args):
        assert args==['issue','verify-check','--config','/config','--issue-id','F152','--round-no','1','--wait','--timeout','30.0']
        return 0,json.dumps({'ok':True,'data':{'state':'running','timed_out':True},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    monkeypatch.setattr(op,'submit',lambda *a:pytest.fail('must not submit'))
    r=call(m.create_server('/config'),'afxx_issue',{'action':'verify-check','issue_id':'F152','round_no':1,'wait':True,'timeout':30})
    assert r.structuredContent['data']['timed_out']


@pytest.mark.parametrize('options',[{}, {'round_no':1,'request_id':'x'}, {'round_no':1,'timeout':10}, {'round_no':1,'full':False}])
def test_verify_check_mcp_constraints(monkeypatch,options):
    async def invoke(*a): pytest.fail('must not execute')
    monkeypatch.setattr(m,'invoke',invoke)
    r=call(m.create_server(),'afxx_issue',dict(action='verify-check',issue_id='F152',**options))
    assert r.isError


def test_readable_job_id_forwarded(monkeypatch):
    async def invoke(args):
        assert args==['status','--config','/config','--request-id','deploy-F152-001']
        return 0,json.dumps({'ok':True,'data':{},'error':None})
    monkeypatch.setattr(m,'invoke',invoke)
    assert not call(m.create_server('/config'),'afxx_job_status',{'request_id':'deploy-F152-001'}).isError


@pytest.mark.parametrize('identity',['.','..','requests','../bad'])
def test_mcp_job_id_reserved_names(identity):
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        call(m.create_server(),'afxx_job_status',{'request_id':identity})


@pytest.mark.parametrize('command,options', [('replay list', []), ('replay show', ['--run', '2']), ('replay compare', ['--runs', '1', '2'])])
def test_replay_history_mcp_forwards_group(monkeypatch, command, options):
    async def invoke(args):
        assert args == [*command.split(), '--config', '/config', *options, '--request-id', 'repair']
        return 0, json.dumps({'ok': True, 'data': {}, 'error': None})
    monkeypatch.setattr(m, 'invoke', invoke)
    monkeypatch.setattr(op, 'submit', lambda *a: pytest.fail('history must not create operations'))
    assert not call(m.create_server('/config'), 'afxx_run', dict(command=command, arguments=options, request_id='repair')).isError


@pytest.mark.parametrize('command', ['replay', 'replay show', 'status'])
def test_replay_and_status_reject_conflicting_group(monkeypatch, command):
    async def forbidden(*a):
        pytest.fail('conflicting request must not execute')
    monkeypatch.setattr(m, 'invoke', forbidden)
    result = call(m.create_server(), 'afxx_run', dict(command=command, request_id='one', arguments=['--request-id', 'two']))
    assert result.structuredContent['error']['code'] == 'REQUEST_CONFLICT'


@pytest.mark.parametrize('arguments', [[], ['--detach']])
def test_mcp_deploy_always_detaches(arguments):
    args, _ = m.prepare('deploy', arguments, None, '/config')
    assert args.count('--detach') == 1


def test_deploy_schema_has_no_issue_binding():
    tools = asyncio.run(m.create_server().list_tools())
    deploy = next(tool for tool in tools if tool.name == "afxx_deploy")
    assert "issue_id" not in deploy.inputSchema["properties"]


def test_summary_mcp_forwards_optional_override(monkeypatch):
    seen = []
    async def invoke(args):
        seen.append(args)
        return 0, json.dumps({'ok': True, 'data': {}, 'error': None})
    monkeypatch.setattr(m, 'invoke', invoke)
    server = m.create_server('/config')
    base = dict(command='issue submit-summary', arguments=['--issue-id', 'F152', '--summary-file', '/summary.yaml'])
    assert not call(server, 'afxx_run', base).isError
    assert '--request-id' not in seen[0]
    assert not call(server, 'afxx_run', dict(base, request_key='manual-summary')).isError
    assert seen[1][-2:] == ['--request-key', 'manual-summary']


def test_case_execution_tool_replaces_execution_show(monkeypatch):
    server = m.create_server("/explicit")
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "afxx_case_execution" in names
    assert "afxx_execution_show" not in names
    async def invoke(args):
        assert args == ["case", "execution", "--config", "/explicit", "--exec-uuid", "exec-0001"]
        return 0, json.dumps({"ok": True, "data": {"exec_uuid": "exec-0001"}})
    monkeypatch.setattr(m, "invoke", invoke)
    result = call(server, "afxx_run", {"command": "case execution", "arguments": ["--exec-uuid", "exec-0001"]})
    assert result.structuredContent["data"]["exec_uuid"] == "exec-0001"


@pytest.mark.parametrize('action,arguments', [
    ('issue release', {'request_id':'old'}),
    ('issue submit-summary', {'request_id':'old'}),
    ('case history', {'request_key':'wrong'}),
    ('issue release', {'request_key':'one', 'arguments':['--request-key','two']}),
])
def test_mcp_mutation_key_rejects_wrong_scope_or_conflict(monkeypatch, action, arguments):
    async def forbidden(*args): pytest.fail('must not invoke CLI')
    monkeypatch.setattr(m, 'invoke', forbidden)
    assert call(m.create_server(), 'afxx_run', dict(command=action, **arguments)).isError


def test_mcp_release_key_optional(monkeypatch):
    async def invoke(args):
        assert '--request-key' not in args and '--request-id' not in args
        return 0, json.dumps({'ok':True,'data':{},'error':None})
    monkeypatch.setattr(m, 'invoke', invoke)
    assert not call(m.create_server(), 'afxx_issue', {'action':'release','issue_id':'F152'}).isError


def test_mcp_replay_has_no_upload_switch():
    tools = asyncio.run(m.create_server().list_tools())
    replay = next(t for t in tools if t.name == 'afxx_replay')
    assert 'upload' not in replay.inputSchema['properties']


def test_group_status_parameters(monkeypatch):
    calls=[]
    async def invoke(args):
        calls.append(args)
        return 0, '{"ok":true,"data":{}}'
    monkeypatch.setattr(m,'invoke',invoke)
    server=m.create_server('/config')
    result=call(server,'afxx_job_status',{'request_id':'repair','task_type':'replay','run':2})
    assert not result.isError
    assert '--type' in calls[0] and 'replay' in calls[0]
    assert calls[0][calls[0].index('--run')+1]=='2'
    assert call(server,'afxx_job_status',{'request_id':'repair','run':2}).isError
