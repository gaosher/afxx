#!/usr/bin/env python3
"""Local preflight, remote job transport and worker dispatch."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from af_forge import bundle as workflow_bundle
from af_forge import common as workflow_common
from af_forge import config as workflow_config
from af_forge import execution as workflow_execution
from af_forge.environment import runtime as workflow_runtime
from af_forge import source as workflow_source


def confirm_submission(record, saved, local, status):
    if status["state"] not in ("queued", "running", "success", "failed"):
        return None
    # Persist acknowledgement before deleting bytes. A crash/partial cleanup must
    # never allow a missing remote task to be relaunched from an incomplete payload.
    saved["remote_accepted"] = True
    workflow_common.write(record, saved)
    if (local / "source.json").is_file():
        return workflow_source.cleanup_snapshot(local, "remote_launch_confirmed")
    return None


def cleanup_after_status(c, job_id, status):
    local = Path(c["local"]["state_dir"]) / job_id
    receipt = local / "request.json"
    if not receipt.is_file():
        return None  # Older tasks are not swept or guessed at.
    submission = workflow_common.read(receipt).get("submission")
    if submission is None:
        return None
    key = submission["request_key"]
    workflow_common.require(
        re.fullmatch(r"[0-9a-f]{64}", key), "invalid local submission receipt"
    )
    record = Path(c["local"]["state_dir"]) / "requests" / (key + ".json")
    with workflow_common.lock(record.with_suffix(".lock")):
        saved = workflow_common.read(record)
        if saved["job"] != job_id or workflow_common.read(local / "config.yaml") != c:
            return None
        return confirm_submission(record, saved, local, status)


def worker(job):
    job = Path(job).resolve()
    os.umask(0o077)

    def interrupted(signum, frame):
        raise RuntimeError("interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        c = workflow_config.validate_config(workflow_common.read(job / "config.yaml"))
        request = workflow_common.read(job / "request.json")
        workflow_common.require_code(
            request.get("command") in ("deploy", "init", "update", "replay", "check"),
            "INVALID_ARGUMENT", "unsupported worker command",
        )
        source = Path(c["remote"]["source_dir"]).resolve()
        for value in (
            c["remote"]["state_dir"],
            c["runtime"]["install_root"],
            c["runtime"]["venv_dir"],
            c["replay"]["results_dir"],
        ):
            other = Path(value).resolve()
            workflow_common.require(
                not workflow_common.inside(other, source)
                and not workflow_common.inside(source, other),
                "resolved remote paths overlap",
            )
        for key in ("llvm_build_dir", "lld_bin"):
            workflow_common.require(
                not workflow_common.inside(Path(c["build"][key]).resolve(), source),
                "resolved toolchain overlaps source",
            )
        workflow_common.phase(job, "starting")
        if request["command"] == "deploy":
            from af_forge.afx import platform as platform_adapter
            with platform_adapter.deployment_lease(c, job):
                workflow_execution.deploy(c, job)
        elif request["command"] in ("init", "update"):
            workflow_common.phase(job, request["command"])
            from af_forge.environment import bootstrap

            workflow_common.phase(job, "python_bootstrap")
            interpreter = bootstrap.prepare(c, job)
            workflow_common.phase(job, request["command"])
            workflow_common.run(
                [
                    interpreter,
                    "-m",
                    "af_forge.environment.python",
                    "_" + request["command"] + "_job",
                    job,
                ],
                cwd=job,
            )
        elif request["command"] == "replay":
            workflow_execution.replay(c, job, request)
        else:
            with (
                workflow_common.lock(workflow_runtime.venv_lock(c), shared=True),
                workflow_common.lock(
                    workflow_runtime.registry(c).with_suffix(".lock"), shared=True
                ),
            ):
                workflow_execution.record_environment(c, job)
                workflow_runtime.venv_preflight(
                    c, workflow_runtime.environment(c), job, "deploy"
                )
        workflow_common.write(
            job / "status.json",
            {
                "job": job.name,
                "state": "success",
                "pid": os.getpid(),
                "artifact": str(
                    job
                    / (
                        {
                            "init": "result.json",
                            "update": "result.json",
                            "deploy": "deployment.json",
                            "replay": "report.json",
                            "check": "python-dependencies.json",
                        }[request["command"]]
                    )
                ),
                "log": str(job / "job.log"),
            },
        )
        return 0
    except Exception as exc:
        previous = (
            workflow_common.read(job / "status.json")
            if (job / "status.json").exists()
            else {}
        )
        workflow_common.write(
            job / "status.json",
            {
                "job": job.name,
                "state": "failed",
                "phase": previous.get("phase"),
                "error": str(exc),
                "error_code": phase_error(previous.get("phase")),
                "exit_code": getattr(exc, "returncode", 1),
                "pid": os.getpid(),
            },
        )
        print(str(exc), file=sys.stderr)
        return 1


def local_preflight(command, dry_run=False):
    """Check local executables and authentication before creating a task."""
    workflow_common.require(
        sys.version_info >= (3, 9), "本地需要 Python 3.9+；请使用 python3 启动。"
    )
    workflow_common.require(
        not (command == "status" and dry_run),
        "status 不支持 --dry-run；查询状态需要连接远端。",
    )
    checks = {}
    if command == "deploy":
        checks["git"] = (["--version"], "请安装 Git 并加入 PATH。")
    if not dry_run:
        checks.update(
            {
                "node": (["--version"], "请安装 Node.js 20+ 并加入 PATH。"),
                "npussh": (
                    ["--version"],
                    "请执行 npm install -g npussh@0.2.4，并确认 npm 全局 bin 目录在 PATH 中。",
                ),
                "ssh": (["-V"], "请安装 OpenSSH client 并加入 PATH。"),
            }
        )
        if command != "status":
            checks.update(
                {
                    "npursync": (
                        ["--version"],
                        "npursync 由 npussh 包提供；请执行 npm install -g npussh@0.2.4，并检查 PATH。",
                    ),
                    "rsync": (["--version"], "请安装系统 rsync 并加入 PATH。"),
                }
            )
    missing = [
        name + "：" + hint
        for name, (_, hint) in checks.items()
        if not shutil.which(name)
    ]
    workflow_common.require_code(
        not missing, "DEPENDENCY_MISSING", "缺少本地依赖：" + "; ".join(missing)
    )
    for name, (args, hint) in checks.items():
        try:
            result = workflow_common.run(
                [name, *args],
                check=False,
                timeout=10,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise workflow_common.WorkflowError(
                "DEPENDENCY_UNUSABLE",
                "本地依赖无法运行或检查超时：" + name + "。" + hint,
            ) from error
        workflow_common.require_code(
            result.returncode == 0,
            "DEPENDENCY_UNUSABLE",
            "本地依赖无法运行：" + name + "。" + hint,
        )
        if name == "node":
            version = re.fullmatch(r"v(\d+)\.\d+\.\d+\s*", result.stdout)
            workflow_common.require_code(
                version and int(version.group(1)) >= 20,
                "DEPENDENCY_UNUSABLE",
                "npussh 需要 Node.js 20+；请升级 node 并检查 PATH。",
            )
    if dry_run:
        return
    try:
        result = workflow_common.run(
            ["npussh", "list"],
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired as error:
        raise workflow_common.WorkflowError(
            "AUTH_CHECK_TIMEOUT",
            "npussh 登录检查超时（30 秒）；请检查网络并运行 npussh list 排查。",
        ) from error
    except OSError as error:
        raise workflow_common.WorkflowError(
            "DEPENDENCY_UNUSABLE", "无法启动 npussh 登录检查；请检查安装和 PATH。"
        ) from error
    if result.returncode:
        # Never echo account listings, credentials, or service response bodies.
        detail = (result.stdout + result.stderr).lower()
        if any(
            value in detail
            for value in ("尚未登录", "重新登录", "invalid_grant", "http 401")
        ):
            raise workflow_common.WorkflowError(
                "AUTH_REQUIRED",
                "npussh 尚未登录或登录已失效；请在本地终端执行 npussh login 后重试。",
            )
        raise workflow_common.WorkflowError(
            "AUTH_SERVICE_UNAVAILABLE",
            "npussh 登录/服务检查失败；请运行 npussh list 检查网络、服务或账号授权；"
            "若提示登录失效，执行 npussh login。",
        )


def phase_error(phase):
    return {
        "platform_verify": "PLATFORM_VERIFY_FAILED",
        "environment_snapshot": "ENVIRONMENT_SNAPSHOT_FAILED",
        "python_dependencies": "REMOTE_DEPENDENCY_FAILED",
        "python_bootstrap": "PYTHON_BOOTSTRAP_FAILED",
        "sync": "SOURCE_SYNC_FAILED",
        "build": "BUILD_FAILED",
        "install": "INSTALL_FAILED",
        "verify_install": "INSTALL_VERIFY_FAILED",
        "replay": "REPLAY_FAILED",
        "replay_prepare": "REPLAY_PREPARE_FAILED",
        "cann_preflight": "CANN_PREPARE_FAILED",
        "cann_install": "CANN_INSTALL_FAILED",
    }.get(phase, "WORKER_FAILED")


REMOTE_JOB = r"""import fcntl,json,os,subprocess,sys,uuid
from pathlib import Path
p=Path(sys.argv[1]); action=sys.argv[2]
def write(path,data):
 tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
 tmp.write_text(json.dumps(data)); tmp.replace(path)
def inspect():
 status=p/'status.json'; intent=p/'launch.json'
 if not status.exists() and not intent.exists():return {'state':'not_found','job':p.name}
 data=json.loads(status.read_text()) if status.exists() else {'state':'unknown','job':p.name}
 launch=json.loads(intent.read_text()) if intent.exists() else {}
 if data['state'] in ('queued','running','unknown'):
  pid=launch.get('pid') or data.get('pid')
  try:
   cmd=Path('/proc',str(pid),'cmdline').read_bytes()
   if not pid or str(p).encode() not in cmd:raise ValueError('worker missing')
  except (OSError,ValueError):
   data.update(state='unknown',error_code='LAUNCH_UNCERTAIN',error='inspect logs; do not resubmit with a new request ID')
 data.update(log=str(p/'job.log'))
 if (p/'request.json').exists():data['task_type']=json.loads((p/'request.json').read_text()).get('command')
 if (p/'source.json').exists():
  cleanup=json.loads((p/'source.json').read_text()).get('snapshot_cleanup')
  if cleanup is not None:data['snapshot_cleanup']=cleanup
 for name in ('report.json','python-dependencies.json','deployment.json','platform.json','result.json'):
  if (p/name).exists():data[name]=json.loads((p/name).read_text())
 return data
if action=='follow':
 import time
 offsets = {'job.log': 0, 'uv.log': 0}
 previous = None
 while True:
  data = inspect()
  stage = (data['state'], data.get('phase'))
  if stage != previous:
   print('[' + (sys.argv[3] if len(sys.argv)>3 else 'init') + '] ' + ' '.join(filter(None, stage)), flush=True)
   previous = stage
  for name, offset in offsets.items():
   path = p/name
   if path.is_file():
    with path.open('rb') as stream:
     stream.seek(offset)
     while True:
      chunk = stream.read(65536)
      if not chunk:break
      sys.stdout.buffer.write(chunk)
     offsets[name] = stream.tell()
    sys.stdout.buffer.flush()
  if stage[0] not in ('running','queued'):
   break
  time.sleep(1)
 sys.exit(0)
if action in ('status','view'):
 if not p.exists():print(json.dumps({'state':'not_found','job':p.name}));sys.exit(0)
else:p.mkdir(parents=True,exist_ok=True,mode=0o700)
with (p/'.submit.lock').open('a') as guard:
 fcntl.flock(guard,fcntl.LOCK_EX)
 data=inspect()
 if action=='launch' and data['state']=='not_found':
  write(p/'launch.json',{'state':'starting','job':p.name})
  write(p/'status.json',{'state':'queued','job':p.name})
  with (p/'job.log').open('ab') as log:
   child=subprocess.Popen([sys.executable,'-m','af_forge.cli','_worker',str(p)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True,cwd=p)
  write(p/'launch.json',{'state':'started','pid':child.pid,'job':p.name})
  data=inspect()
 if action=='view':
  try:data=status_view(data,**json.loads(sys.argv[3]))
  except ValueError as error:data={'state':'failed','query_error':str(error)}
 print(json.dumps(data))
"""


def remote(c, argv):
    try:
        result = workflow_common.run(
            [
                "npussh",
                c["connection"]["environment"],
                shlex.join([str(a) for a in argv]),
            ],
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            timeout=60,
        )
        data = json.loads(result.stdout)
        workflow_common.require(
            isinstance(data, dict)
            and data.get("state")
            in {"not_found", "queued", "running", "success", "failed", "unknown"},
            "invalid remote response",
        )
        return data
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise workflow_common.WorkflowError(
            "REMOTE_UNAVAILABLE",
            "远端响应无法确认；使用相同 --request-id 重试或查询原 job。",
        ) from error


def query_job(c, job_id, action="status"):
    return remote(
        c,
        [
            "python3",
            "-c",
            REMOTE_JOB,
            str(Path(c["remote"]["state_dir"]) / job_id),
            action,
        ],
    )


def submit(c, request, request_id, dry_run):
    state = Path(c["local"]["state_dir"])
    identity = workflow_common.digest(request_id)
    record = state / "requests" / (identity + ".json")
    fingerprint = workflow_common.digest(
        {"config": c, "request": request, "dry_run": dry_run}
    )
    with workflow_common.lock(record.with_suffix(".lock")):
        reused = record.exists()
        if reused:
            saved = workflow_common.read(record)
            workflow_common.require_code(
                saved["fingerprint"] == fingerprint,
                "REQUEST_CONFLICT",
                "request-id 已绑定其他配置或参数；新任务请使用新的 request-id。",
            )
            workflow_common.require_code(
                saved["prepared"],
                "PREPARATION_INCOMPLETE",
                "首次冻结任务被中断；检查本地目录后使用新的 request-id，不复用不完整快照。",
            )
        else:
            saved = {
                "job": uuid.uuid4().hex,
                "fingerprint": fingerprint,
                "prepared": False,
            }
            workflow_common.write(record, saved)
        job_id = saved["job"]
        local = state / job_id
        data = {
            "request_id": request_id,
            "job": job_id,
            "local": str(local),
            "remote": str(Path(c["remote"]["state_dir"]) / job_id),
            "dry_run": dry_run,
            "reused": reused,
        }
        try:
            if not reused:
                local.mkdir(parents=True, mode=0o700)
                workflow_common.write(local / "config.yaml", c)
                workflow_common.write(
                    local / "request.json",
                    dict(request, submission={"request_key": identity}),
                )
                if request["command"] == "deploy":
                    workflow_common.write(
                        local / "source.json",
                        workflow_source.snapshot(c, local / "snapshot"),
                    )
                workflow_bundle.copy_to(local)
                saved["prepared"] = True
                workflow_common.write(record, saved)
            if dry_run:
                return dict(data, state="prepared")
            status = query_job(c, job_id, "prepare")
            if status["state"] == "not_found":
                workflow_common.require_code(
                    not saved.get("remote_accepted"),
                    "REMOTE_TASK_LOST",
                    "已确认启动的远端任务记录丢失；禁止重新启动，请检查远端目录。",
                )
                if request["command"] == "deploy":
                    workflow_common.require_code(
                        (local / "snapshot").is_dir(),
                        "SNAPSHOT_UNAVAILABLE",
                        "本地冻结快照已不存在；不能使用此 request-id 重新上传。",
                    )
                    workflow_source.verify(
                        local / "snapshot", workflow_common.read(local / "source.json")
                    )
                try:
                    workflow_common.run(
                        [
                            "npursync",
                            "-a",
                            "--checksum",
                            str(local) + "/",
                            c["connection"]["environment"] + ":" + data["remote"] + "/",
                        ],
                        stdout=sys.stderr,
                        stderr=sys.stderr,
                        timeout=600,
                    )
                except (OSError, subprocess.SubprocessError) as error:
                    raise workflow_common.WorkflowError(
                        "TRANSFER_FAILED", "任务上传失败；使用相同 request-id 重试。"
                    ) from error
                status = query_job(c, job_id, "launch")
            cleanup = confirm_submission(record, saved, local, status)
            return dict(
                data,
                state=status["state"],
                status=status,
                local_snapshot_cleanup=cleanup,
            )
        except workflow_common.WorkflowError as error:
            error.data = data
            raise
        except Exception as error:
            raise workflow_common.WorkflowError(
                "PREPARATION_FAILED", str(error), data
            ) from error


def query_evidence(c, job_id, action, *, path=None, offset=0, limit=8192, sha256=None):
    import inspect
    from af_forge.afx import replay
    workflow_common.require_code(offset >= 0 and 1 <= limit <= 65536,
                                 "INVALID_ARGUMENT", "offset >= 0; limit must be 1..65536")
    code = "import json,os,re,sys\nfrom pathlib import Path\n"
    for function in (replay.collect_evidence, replay.failure_index, replay.associate_evidence, replay.read_evidence):
        code += inspect.getsource(function) + "\n"
    code += """
job = Path(sys.argv[1])
options = json.loads(sys.argv[2])
try:
 if options['action'] == 'read':
  value = read_evidence(job, options['path'], options['offset'], options['limit'], options['sha256'])
 else:
  report = json.loads((job/'report.json').read_text())
  value = collect_evidence(job, Path(report['results_dir']), report.get('resources',{}).get('dump', False), options['offset'])
  associate_evidence(value, report.get('attempts', []))
 print(json.dumps({'state':'success','evidence':value}))
except (OSError, ValueError, KeyError) as error:
 print(json.dumps({'state':'failed','error_code':'EVIDENCE_UNAVAILABLE','error':str(error)}))
"""
    value = remote(c, ["python3", "-c", code, str(Path(c["remote"]["state_dir"]) / job_id),
                       json.dumps(dict(action=action, path=path, offset=offset, limit=limit, sha256=sha256))])
    workflow_common.require_code(value['state'] == 'success', value.get('error_code', 'EVIDENCE_UNAVAILABLE'), value.get('error', 'evidence unavailable'))
    return value['evidence']


def query_status(c, job_id, *, full=False, attempt_id=None, fields=None):
    import inspect
    from af_forge.job_view import status_view
    from af_forge.afx.replay import failure_index
    code = inspect.getsource(failure_index) + "\n" + inspect.getsource(status_view) + "\n" + REMOTE_JOB
    data = remote(c, ["python3", "-c", code, str(Path(c["remote"]["state_dir"]) / job_id),
                      "view", json.dumps(dict(full=full, attempt_id=attempt_id, fields=fields))])
    workflow_common.require_code("query_error" not in data, "ATTEMPT_FIELD_NOT_FOUND" if data.get("query_error", "").startswith("attempt field") else "ATTEMPT_NOT_FOUND",
                                 data.get("query_error", "attempt not found"))
    return data


def status_cursor(data):
    return workflow_common.digest({key: value for key, value in data.items()
                                   if key not in {"cursor", "snapshot_cleanup", "local_snapshot_cleanup"}})


def wait_status(c, job_id, *, since=None, timeout=30):
    """Bounded long poll: one changed/terminal result, or a tiny timeout receipt."""
    import time
    workflow_common.require_code(0 < timeout <= 50, "INVALID_ARGUMENT", "wait timeout must be in (0, 50] seconds")
    workflow_common.require_code(since is None or re.fullmatch(r"[0-9a-f]{64}", since),
                                 "INVALID_ARGUMENT", "invalid status cursor")
    deadline = time.monotonic() + timeout
    baseline = since
    while True:
        data = query_status(c, job_id)
        workflow_common.require_code(data["state"] != "not_found", "JOB_NOT_FOUND", "未找到任务：" + job_id)
        cursor = status_cursor(data)
        if data["state"] in {"success", "failed", "unknown"}:
            from af_forge import credentials
            data = credentials.complete_init(c, job_id, data)
            result = localize_replay(c, job_id, data)
            if "evidence_path" in result:
                return result
            return dict(result, cursor=cursor, event="terminal" if data["state"] != "unknown" else "attention")
        if baseline is not None and cursor != baseline:
            return dict(data, cursor=cursor, event="changed")
        baseline = cursor
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"job_id": job_id, "event": "timeout", "cursor": cursor}
        time.sleep(min(2, remaining))


def localize_replay(c, job_id, data):
    if data.get("state") not in {"success", "failed"} or not ("replay" in data or data.get("task_type") == "replay"):
        return data
    from af_forge import evidence_bundle
    directory = evidence_bundle.download(c, job_id, remote)
    return evidence_bundle.terminal_result(directory, dict(data, job_id=job_id))
