#!/usr/bin/env python3
"""CLI entry point for AF development workflows."""

import argparse
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from afxx.afx import platform
from afxx.afx import replay as replay_adapter
from afxx import common as workflow_common
from afxx import config as workflow_config
from afxx import jobs as workflow_jobs
from afxx import credentials
from afxx import replay_runs
from afxx.job_view import status_view
from afxx.environment import runtime as workflow_runtime


class AgentParser(argparse.ArgumentParser):
    def error(self, message):
        raise workflow_common.WorkflowError("INVALID_ARGUMENT", message)


def response(command, data=None, error=None):
    if command in ("init", "update") and data and "job" in data:
        status = data.get("status") or {}
        summary = {
            key: data[key] for key in ("job", "request_id", "state", "auth", "config_save") if key in data
        }
        if data.get("dry_run"):
            summary.update(dry_run=True, local=data["local"])
        if data.get("reused"):
            summary["reused"] = True
        for key in ("phase", "artifact", "log", "error_code", "error"):
            if key in status:
                summary[key] = status[key]
        if command == "update":
            report = status.get("result.json", {})
            for key in ("installed", "components", "rollback"):
                if key in report:
                    summary[key] = report[key]
        data = summary
    if (
        command == "issue-claim"
        and error is None
        and isinstance(data, dict)
        and "issue_id" in data
    ):
        fields = (
            "issue_id",
            "status",
            "title",
            "backend",
            "case_count",
            "lease_expires_at",
            "request_id",
        )
        data = {key: data[key] for key in fields if key in data}
    if isinstance(data, dict):
        data = dict(data)
        if "job" in data:
            data["job_id"] = data.pop("job")
        if isinstance(data.get("status"), dict):
            status = status_view(data.pop("status"))
            data.update({key: value for key, value in status.items() if key not in data})
    if command == "status" and isinstance(data, dict) and "job_id" in data:
        data["request_id"] = data.pop("job_id")
    return {
        "schema_version": 1,
        "ok": error is None,
        "command": command.replace("-", " ", 1) if command and command.startswith(("issue-", "execution-", "config-", "evidence-", "fuzz-", "skills-", "client-", "case-", "platform-", "retest-", "replay-")) else command,
        "data": data,
        "error": None if error is None else {"code": error.code, "message": str(error)},
    }


def parse_arguments():
    parser = AgentParser(
        prog="afxx", usage="%(prog)s <命令> [参数]",
        description="AFXX · 环境管理、源码部署与 AFX 验证", allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('afxx')}", help="显示安装版本并退出")
    commands = parser.add_subparsers(dest="command", required=True, title="命令", metavar="", prog="afxx")
    groups = {}
    env_jobs = (
        "init",
        "update",
        "check",
    )
    jobs = (
        "deploy",
        "replay",
        "fuzz-run",
    )
    issues = (
        "issue-list",
        "issue-cases",
        "issue-claim",
        "issue-preview",
        "issue-status",
        "issue-wait",
        "issue-verify",
        "issue-verify-check",
        "issue-renew",
        "issue-release",
        "issue-detail",
        "issue-tasks",
        "issue-regression-preview",
        "issue-submit-summary",
    )
    clients = tuple("client-" + action for action in ("status", "start", "stop", "restart"))
    queries = ("platform-me", "platform-summary", "case-list", "case-spec", "case-history", "retest-list")
    descriptions = {
        "issue-list": "分页查询平台问题",
        "issue-cases": "分页查询问题关联用例",
        "platform-me": "查询当前 token 的用户身份",
        "platform-summary": "查询平台运行概况",
        "issue-release": "释放已领取问题的租约",
        "case-list": "筛选并分页查询用例及最近执行",
        "case-spec": "查看用例保存的图元数据",
        "case-history": "分页查询用例执行历史",
        "retest-list": "查询当前用户的手动复测批次",
        **{name: "管理远端唯一 AFX client：" + name.split("-", 1)[1] for name in clients},
        "init": "远端环境：初始化 Python 和 CANN 环境",
        "update": "远端环境：显式更新指定组件；无组件时查询当前版本",
        "check": "远端环境：检查运行环境及 Python 依赖",
        "deploy": "远端工作流：同步本地源码，编包并安装 AF",
        "replay": "远端工作流：通过 afx fuzz replay 复跑 seed，按 request_id 归组",
        "fuzz-run": "随机测试：调用远端 AFX fuzz run 并保存结果",
        "fuzz-status": "随机测试：查询 AFX 队列与运行进度",
        "fuzz-stop": "随机测试：请求 AFX 停止本次随机测试",
        "status": "远端工作流：查询 workflow 任务状态",
        "issue-claim": "问题验证：从 AFX 平台领取待修复问题",
        "issue-preview": "问题验证：查看复现用例和失败摘要",
        "issue-status": "问题验证：查询平台验证和回归结果",
        "issue-wait": "问题验证：轮询等待验证和回归结果",
        "issue-verify-check": "只读检查验证和关联回归，可等待并汇总失败任务",
        "issue-verify": "问题验证：针对已部署的 AF 提交验证",
        "issue-renew": "问题验证：续期已领取问题的租约",
        "issue-detail": "问题查询：查看生命周期、租约和历史轮次",
        "issue-tasks": "问题查询：查看指定轮次的任务与失败信息",
        "issue-regression-preview": "问题查询：预览平台回归候选集",
        "issue-submit-summary": "问题归档：提交已检视的本地修复总结",
        "execution-show": "执行查询：查看执行详情、Shape 和日志",
        "evidence-list": "定位证据：分页列出远端复测产物与失败关联",
        "evidence-read": "定位证据：按字节分页读取日志、IR 和代码",
        "doctor": "本地检查：检查配置、连接工具及登录状态",
    }
    for name in (*env_jobs, *jobs, *issues, *clients, *queries, "fuzz-status", "fuzz-stop", "execution-show", "evidence-list", "evidence-read", "status", "doctor"):
        parent = commands
        leaf = name
        if name in env_jobs or name.startswith(("issue-", "execution-", "config-", "evidence-", "fuzz-", "skills-", "client-", "case-", "platform-", "retest-")):
            group = "env" if name in env_jobs else name.split("-", 1)[0]
            leaf = name if group == "env" else name.split("-", 1)[1]
            if group not in groups:
                summary = {
                    "platform": "查询平台身份与运行概况",
                    "case": "查询用例图及执行历史",
                    "retest": "查询手动复测批次",
                    "client": "查询、启动、停止和重启远端 AFX client",
                    "fuzz": "运行、查询和停止远端 AFX 随机测试",
                    "env": "管理远端 Python、CANN 和依赖环境",
                    "issue": "领取问题、提交验证及查询回归结果",
                    "execution": "查询平台执行详情和日志",
                    "evidence": "读取远端 replay 定位证据",
                }[group]
                group_parser = commands.add_parser(
                    group, help=summary, description=summary,
                    usage=f"%(prog)s <操作> [参数]", allow_abbrev=False,
                )
                groups[group] = group_parser.add_subparsers(
                    dest="action", required=True, title="操作", metavar="", prog=f"afxx {group}",
                )
            parent = groups[group]
        cmd = parent.add_parser(leaf, help=descriptions[name], description=descriptions[name], allow_abbrev=False)
        cmd.set_defaults(command=name)
        cmd.add_argument(
            "--config",
            default=workflow_config.default_config_path(),
            help="配置文件（默认 ~/.config/afxx/config.yaml）",
        )
        if name in ("init", "update", *jobs) or name == "doctor":
            cmd.add_argument("--dry-run", action="store_true")
        if name in jobs or name in ("issue-claim", "issue-submit-summary", "issue-release"):
            cmd.add_argument("--request-id", required=name in ("issue-submit-summary", "issue-release"))
        if name in clients:
            cmd.add_argument("--timeout", type=float, default=300, help="等待生命周期完成的秒数，默认 300；超时不强杀")
        if name in ("init", "update", "deploy"):
            cmd.add_argument("--detach", action="store_true", help="提交后立即返回，不跟随远端日志")
        if name == "deploy":
            cmd.add_argument("--coordinate-client", action="store_true", help="安装前停止目标 AFX client，成功后恢复运行或空闲状态；不恢复原队列")
        if name == "update":
            cmd.add_argument("--save-config", action="store_true", help="成功后将实际版本回写本次配置，保留其他字段及注释")
            cmd.add_argument("--torch", help="目标 torch 版本；变更版本时必须同时指定 --torch-npu")
            cmd.add_argument("--torch-npu", nargs="?", const="latest", help="Daily 日期 YYYYMMDD 或 latest；省略值默认 latest，匹配当前/目标 torch")
            cmd.add_argument("--afx", nargs="?", const="latest", help="AFX 版本或 latest；省略值默认 latest")
            cmd.add_argument("--cann", nargs="?", const="latest", help="Toolkit 发布日期 YYYYMMDD 或 latest；省略值默认 latest")
            cmd.add_argument("--ops", action="store_true", help="同时更新同批次 OPS，必须与 --cann 一起使用")
        if name == "issue-preview":
            cmd.add_argument(
                "--full", action="store_true", help="show full preview details"
            )
        if name == "deploy" or (name in issues and name != "issue-list"):
            cmd.add_argument("--issue-id", required=name not in ("deploy", "issue-verify"),
                             help="AFX platform issue, e.g. F152")
        if name in ("issue-wait", "issue-status", "issue-verify-check"):
            cmd.add_argument("--round-no", type=int, required=True, help="verification round returned by issue verify")
        if name == "issue-verify-check":
            cmd.add_argument("--wait", action="store_true")
            cmd.add_argument("--timeout", type=float)
        if name == "issue-status":
            cmd.add_argument("--include-tasks", action="store_true", help="include verification and associated regression tasks")
        if name == "issue-submit-summary":
            cmd.add_argument("--summary-file", required=True, help="reviewed local YAML knowledge fields")
        if name == "issue-tasks":
            cmd.add_argument("--round-no", type=int, required=True, help="verify or regression round number")
        if name in ("case-spec", "case-history"):
            cmd.add_argument("--case-key", required=True)
        if name == "case-list":
            for option in ("backend", "arch", "q"):
                cmd.add_argument("--" + option)
            cmd.add_argument("--status", choices=("pass", "fail", "function_fail", "accuracy_fail"))
            selector = cmd.add_mutually_exclusive_group()
            selector.add_argument("--case-key")
            selector.add_argument("--case-id", type=int)
        if name in ("case-history", "case-list"):
            cmd.add_argument("--after", type=int)
            cmd.add_argument("--limit", type=int, default=50)
        if name == "issue-list":
            for option in ("dimension", "backend", "status", "q", "after"):
                cmd.add_argument("--" + option)
            cmd.add_argument("--limit", type=int, default=50)
        if name == "issue-cases":
            cmd.add_argument("--role", choices=("affected", "regression"), default="affected")
            cmd.add_argument("--after", type=int)
        if name == "platform-summary":
            cmd.add_argument("--scope", choices=("mine", "all"), default="mine")
        if name == "execution-show":
            cmd.add_argument("--exec-uuid", required=True, help="AFX execution UUID, not numeric exec_id")
        if name == "issue-wait":
            cmd.add_argument("--timeout", type=float, default=3600)
        if name in ("issue-verify", "fuzz-status", "fuzz-stop"):
            cmd.add_argument("--job-id", "--job", dest="job", required=True)
        if name in ("evidence-list", "evidence-read"):
            cmd.add_argument("--request-id", required=True)
            cmd.add_argument("--run", type=int, help="replay run; defaults to latest")
            cmd.add_argument("--offset", type=int, default=0)
        if name == "evidence-read":
            cmd.add_argument("--path", required=True, help="absolute remote artifact path")
            cmd.add_argument("--limit", type=int, default=8192, help="bytes, maximum 65536")
            cmd.add_argument("--sha256", help="hash from previous read; reject changed file")
        if name == "status":
            cmd.add_argument("--request-id", required=True)
            detail = cmd.add_mutually_exclusive_group()
            detail.add_argument("--attempt", help="read one AFX attempt (compact fields by default)")
            detail.add_argument("--wait", action="store_true", help="return only changes or terminal state")
            cmd.add_argument("--fields", help="comma-separated attempt fields; supports dotted paths")
            cmd.add_argument("--since", help="cursor from previous status; requires --wait")
            cmd.add_argument("--timeout", type=float, help="wait timeout in seconds, default 30, maximum 50")
        if name == "fuzz-run":
            cmd.add_argument("--count", type=int, help="生成用例数；省略则持续运行，使用 fuzz stop 停止")
            cmd.add_argument("--device", action="append", help="逻辑 NPU，如 npu:0；可重复，默认由 AFX 使用可见多卡")
            cmd.add_argument("--workers", type=int, help="AFX 并发 worker 上限；默认由 AFX 决定")
            cmd.add_argument("--compile-threads", type=int, help="AFX 编译线程数；默认由 AFX 决定")
            cmd.add_argument("--ops", help="AFX 算子过滤，如 add,sum,reshape")
            cmd.add_argument("--backend", action="append", help="增加 AFX 测试后端；可重复，默认 CPU 和 AF-MLIR")
        if name == "replay":
            cmd.add_argument("--dump", action="store_true", help="开启 AFX 逐任务 dump，产物通过 evidence list/read 读取")
            seeds = cmd.add_mutually_exclusive_group()
            seeds.add_argument("--seed")
            seeds.add_argument("--seeds-file")
            uploads = cmd.add_mutually_exclusive_group()
            uploads.add_argument("--upload", action="store_true", default=None)
            uploads.add_argument("--no-upload", dest="upload", action="store_false")
            history = cmd.add_subparsers(dest="replay_action")
            for action in ("list", "show", "compare"):
                query = history.add_parser(action, allow_abbrev=False)
                query.set_defaults(command="replay-" + action)
                query.add_argument("--config", default=argparse.SUPPRESS)
                query.add_argument("--request-id", required=True)
                if action == "show":
                    query.add_argument("--run", type=int)
                if action == "compare":
                    query.add_argument("--runs", type=int, nargs=2, required=True)
    config_parser = commands.add_parser(
        "config", help="本地配置：生成和管理配置", description="本地配置：生成和管理配置",
        usage="%(prog)s <操作> [参数]", allow_abbrev=False,
    )
    config_commands = config_parser.add_subparsers(
        dest="action", required=True, title="操作", metavar="", prog="afxx config",
    )
    template = config_commands.add_parser("init", help="生成带注释的配置模板", allow_abbrev=False)
    template.set_defaults(command="config-init")
    template.add_argument("--source", required=True, help="local graph-autofusion repository")
    template.add_argument("--output", default=workflow_config.default_config_path(),
                          help="输出路径（默认 ~/.config/afxx/config.yaml；不覆盖已有文件）")
    skills_parser = commands.add_parser("skills", help="安装本地 agent skills", allow_abbrev=False)
    skills_commands = skills_parser.add_subparsers(dest="action", required=True)
    skill_install = skills_commands.add_parser("install", help="安装随 AFXX 发布的 skills", allow_abbrev=False)
    skill_install.set_defaults(command="skills-install")
    skill_install.add_argument("--agent", choices=["codex"], required=True)
    mcp_parser = commands.add_parser("mcp", help="启动 stdio MCP 服务", allow_abbrev=False)
    mcp_parser.add_argument("--config", help="默认配置文件覆盖路径")
    commands.add_parser("help", help="显示全部命令及功能说明", allow_abbrev=False)
    parser.set_defaults(
        issue_id=None,
        job=None,
        request_id=None,
        dry_run=False,
        detach=False,
        seeds_file=None,
        deployment=None,
        upload=None,
    )
    args = parser.parse_args()
    if args.command == "replay":
        workflow_common.require_code(args.seed or args.seeds_file, "INVALID_ARGUMENT", "replay requires --seed or --seeds-file")
    if args.command == "status":
        args.job = args.request_id
    if args.command.startswith("replay-"):
        workflow_common.require_code(not (args.dump or args.seed or args.seeds_file or args.dry_run or args.upload is not None),
                                     "INVALID_ARGUMENT", "replay 查询不接受执行参数")
    if args.command == "help":
        parser.print_help()
        parser.exit()
    return args


def main():
    os.umask(0o077)
    if len(sys.argv) == 5 and sys.argv[1] == "_python_check":
        return workflow_runtime.check_main(sys.argv[2:])
    if len(sys.argv) == 3 and sys.argv[1] == "_worker":
        return workflow_jobs.worker(sys.argv[2])
    args = parse_arguments()
    if args.command == "skills-install":
        from afxx.skills import install
        return install()
    if args.command == "config-init":
        return workflow_config.write_template(args.source, args.output)
    if args.issue_id is not None:
        platform.validate_issue_id(args.issue_id)
    if args.job is not None:
        workflow_common.validate_job_id(args.job)
    workflow_common.require_code(
        args.request_id is None
        or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", args.request_id),
        "INVALID_ARGUMENT",
        "request-id 必须为 1–128 位字母、数字、下划线、点或连字符。",
    )
    try:
        c = workflow_config.config(args.config)
    except workflow_common.WorkflowError:
        raise
    except FileNotFoundError as error:
        raise workflow_common.WorkflowError(
            "CONFIG_INVALID", f"配置文件不存在：{args.config}；请运行 afxx config init --source <源码目录>，或用 --config 指定已有配置。"
        ) from error
    except (ValueError, OSError, TypeError) as error:
        raise workflow_common.WorkflowError("CONFIG_INVALID", str(error)) from error
    if args.command in ("init", "update"):
        workflow_common.require_code(
            "python_env" in c,
            "CONFIG_INVALID",
            "init/update requires python_env configuration",
        )
    if args.command == "check":
        return workflow_jobs.environment_check(c)
    if args.command.startswith("replay-"):
        workflow_jobs.local_preflight("status")
        if args.command == "replay-list":
            return replay_runs.list_runs(c, args.request_id)
        if args.command == "replay-show":
            return replay_runs.show(c, args.request_id, args.run)
        return replay_runs.compare(c, args.request_id, *args.runs)
    if args.command.startswith("client-"):
        workflow_common.require_code(math.isfinite(args.timeout) and 0 < args.timeout <= 3600,
                                     "INVALID_ARGUMENT", "client timeout must be in (0, 3600]")
        return workflow_jobs.client_control(c, args.command.split("-", 1)[1], args.timeout)
    if args.command == "platform-me":
        return platform.platform_me(c)
    if args.command == "platform-summary":
        return platform.platform_summary(c, scope=args.scope)
    if args.command == "case-list":
        return platform.case_list(c, **{key: getattr(args, key) for key in ("backend", "status", "arch", "case_key", "case_id", "q", "after", "limit")})
    if args.command == "case-spec":
        return platform.case_spec(c, args.case_key)
    if args.command == "case-history":
        return platform.case_history(c, args.case_key, after=args.after, limit=args.limit)
    if args.command == "retest-list":
        return platform.retest_list(c)
    if args.command == "execution-show":
        return platform.execution_detail(c, args.exec_uuid)
    if args.command.startswith("issue-"):
        workflow_config.platform_settings(c)
        if args.command == "issue-list":
            return platform.issue_list(c, **{key: getattr(args, key) for key in ("dimension", "backend", "status", "q", "after", "limit")})
        if args.command == "issue-cases":
            return platform.issue_cases(c, args.issue_id, role=args.role, after=args.after)
        if args.command == "issue-claim":
            request_id = args.request_id or uuid.uuid4().hex
            if args.request_id is None:
                print(f"issue claim --request-id {request_id}", file=sys.stderr, flush=True)
            return platform.claim(c, request_id, issue_id=args.issue_id)
        if args.command == "issue-verify-check":
            return platform.verify_check(c, args.issue_id, args.round_no, wait=args.wait, timeout=args.timeout)
        if args.command == "issue-release":
            return platform.release(c, args.issue_id, args.request_id)
        if args.command == "issue-submit-summary":
            return platform.summarize(c, args.issue_id, args.summary_file, args.request_id)
        if args.command == "issue-detail":
            return platform.issue_detail(c, args.issue_id)
        if args.command == "issue-tasks":
            return platform.issue_tasks(c, args.issue_id, args.round_no)
        if args.command == "issue-regression-preview":
            return platform.regression_preview(c, args.issue_id)
        if args.command == "issue-renew":
            return platform.renew(c, platform.claimed(c, args.issue_id))
        if args.command == "issue-preview":
            return platform.preview(c, args.issue_id, full=args.full)
        if args.command in ("issue-status", "issue-wait"):
            if args.command == "issue-wait":
                workflow_common.require_code(
                    math.isfinite(args.timeout) and args.timeout > 0,
                    "INVALID_ARGUMENT",
                    "invalid timeout",
                )
                return platform.wait(c, args.issue_id, args.round_no, args.timeout)
            return platform.poll(c, args.issue_id, args.round_no, include_tasks=True) if args.include_tasks else platform.poll(c, args.issue_id, args.round_no)
        workflow_jobs.local_preflight("status")
        directory = str(Path(c["remote"]["state_dir"]) / args.job)
        lease = platform.claimed(c, args.issue_id) if args.issue_id else None
        context = {key: lease[key] for key in ("id", "lease_id")} if lease else None
        code = "import sys,json; sys.path.insert(0,sys.argv[1]); from afxx.afx import platform as p; print(json.dumps(p.retry_remote(sys.argv[1],json.loads(sys.argv[2]))))"
        result = workflow_jobs.remote(
            c, [workflow_runtime.python(c), "-c", code, directory, json.dumps(context)]
        )
        workflow_common.require_code(
            result["state"] == "success",
            result.get("error_code", "PLATFORM_VERIFY_FAILED"),
            result.get("error", "verify failed"),
        )
        return result["verification"]
    workflow_jobs.local_preflight(
        "deploy" if args.command == "doctor" else "status" if args.command.startswith("evidence-") or args.command in ("fuzz-status", "fuzz-stop") else args.command, args.dry_run
    )
    if args.command in ("fuzz-status", "fuzz-stop"):
        directory = str(Path(c["remote"]["state_dir"]) / args.job)
        code = "import sys,json; sys.path.insert(0,sys.argv[1]); from afxx.afx.fuzz import control; print(json.dumps(control(sys.argv[1],sys.argv[2])))"
        return workflow_jobs.remote(c, ["python3", "-c", code, directory, args.command.split("-", 1)[1]])
    if args.command == "doctor":
        return {
            "config": "valid",
            "dependencies": "passed",
            "login": "skipped" if args.dry_run else "passed",
            "dry_run": args.dry_run,
            "effective": {
                key: c[key] for key in ("local", "remote", "runtime", "build", "replay")
            },
        }
    if args.command in ("evidence-list", "evidence-read"):
        return replay_runs.evidence(c, args.request_id, args.run, args.command.split("-", 1)[1],
            path=getattr(args, "path", None), offset=args.offset,
            limit=getattr(args, "limit", 8192), sha256=getattr(args, "sha256", None))
    if args.command == "status":
        workflow_common.require_code(not args.fields or args.attempt is not None,
                                     "INVALID_ARGUMENT", "--fields requires --attempt")
        workflow_common.require_code(args.wait or (args.since is None and args.timeout is None),
                                     "INVALID_ARGUMENT", "--since/--timeout require --wait")
        fields = args.fields.split(",") if args.fields is not None else None
        workflow_common.require_code(fields is None or all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", name) for name in fields),
                                     "INVALID_ARGUMENT", "invalid attempt fields")
        if args.wait:
            return workflow_jobs.wait_status(c, args.job, since=args.since,
                                             timeout=args.timeout if args.timeout is not None else 30)
        data = workflow_jobs.query_status(c, args.job, attempt_id=args.attempt, fields=fields)
        if args.attempt is None:
            data["cursor"] = workflow_jobs.status_cursor(data)
        workflow_common.require_code(
            data["state"] != "not_found", "JOB_NOT_FOUND", "未找到任务：" + args.job
        )
        try:
            cleanup = workflow_jobs.cleanup_after_status(c, args.job, data)
        except (OSError, ValueError, KeyError) as error:
            cleanup = {"state": "failed", "error": str(error)}
        if cleanup is not None:
            data["local_snapshot_cleanup"] = cleanup
        if args.attempt is None:
            data = credentials.complete_init(c, args.job, data)
            data = workflow_jobs.complete_config_save(c, args.job, data)
        return workflow_jobs.localize_replay(c, args.job, data) if args.attempt is None else data
    values = []
    if args.command == "replay":
        if args.dump:
            c["replay"]["dump"] = True
        values = replay_adapter.requested_seeds(args.seed, args.seeds_file)
    request = {
        "command": args.command,
        "seeds": values,
        "batch": bool(args.seeds_file),
        "deployment": args.deployment,
        "upload": args.upload,
    }
    if args.command == "replay":
        request.pop("deployment")
        return replay_runs.submit(c, request, args.request_id or uuid.uuid4().hex, args.dry_run)
    if args.command == "fuzz-run":
        for name in ("count", "workers", "compile_threads"):
            value = getattr(args, name)
            workflow_common.require_code(value is None or value > 0, "INVALID_ARGUMENT", f"--{name.replace('_', '-')} must be positive")
        request["fuzz"] = {key: getattr(args, key) for key in
                           ("count", "workers", "compile_threads", "device", "backend", "ops")}
    if args.command == "deploy" and args.coordinate_client:
        request["coordinate_client"] = True
    if args.command == "deploy" and args.issue_id:
        lease = platform.claimed(c, args.issue_id)
        request["issue"] = {key: lease[key] for key in ("id", "lease_id")}
    if args.command == "update":
        from afxx.environment.python import validate_selection
        request["updates"] = validate_selection({key: getattr(args, key) for key in
                                                 ("torch", "torch_npu", "afx", "cann", "ops")})
        if args.save_config:
            from afxx.config_save import prepare
            request["save_config"] = prepare(args.config, request["updates"])
    if args.command == "init" and not args.dry_run:
        credentials.prepare(c)
    if args.command in ("init", "update"):
        data = workflow_jobs.submit_environment(c, request, args.dry_run)
    else:
        data = workflow_jobs.submit(c, request, args.request_id or uuid.uuid4().hex, args.dry_run)
    if (
        args.command
        in ("init", "update", "deploy")
        and not args.dry_run
        and not getattr(args, "detach", False)
    ):
        print(
            f"{args.command} job {data['job']} (Ctrl+C stops viewing)",
            file=sys.stderr,
            flush=True,
        )
        command = [
            "python3",
            "-c",
            workflow_jobs.REMOTE_JOB,
            data["remote"],
            "follow",
            args.command,
        ]
        try:
            workflow_common.run(
                ["npussh", c["connection"]["environment"], shlex.join(command)],
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            status = workflow_jobs.query_job(c, data["job"])
        except KeyboardInterrupt:
            raise workflow_common.WorkflowError(
                "FOLLOW_INTERRUPTED", "已停止查看；可用 status 查询此任务。", data
            ) from None
        except (
            OSError,
            subprocess.SubprocessError,
            workflow_common.WorkflowError,
        ) as error:
            raise workflow_common.WorkflowError(
                "REMOTE_UNAVAILABLE", "日志连接中断，请用 status 查询此任务。", data
            ) from error
        data.update(state=status["state"], status=status)
        if status["state"] != "success":
            raise workflow_common.WorkflowError(
                status.get("error_code", "WORKER_FAILED"),
                status.get("error", args.command + " 未确认成功"),
                data,
            )
    if args.command == "init" and data.get("state") == "success" and not args.dry_run:
        data = credentials.complete_init(c, data["job"], dict(data, task_type="init"))
    if args.command == "update" and not args.dry_run:
        data = workflow_jobs.complete_config_save(c, data["job"], data)
    return data


def cli():
    if len(sys.argv) > 1 and sys.argv[1] == "mcp":
        try:
            from afxx.mcp_server import main as mcp_main
        except ModuleNotFoundError as error:
            if error.name != "mcp":
                raise
            print("MCP 依赖未安装，请在当前 Python 环境执行：python -m pip install 'afxx[mcp]'", file=sys.stderr)
            return 2

        mcp_main(sys.argv[2:])
        return 0
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ("issue", "execution", "config", "evidence", "fuzz", "skills", "client", "case", "platform", "retest", "replay") and len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
        command += "-" + sys.argv[2]
    if command in ("_worker", "_python_check"):
        return main()
    indent = 2 if sys.stdout.isatty() else None
    try:
        data = main()
        print(
            json.dumps(response(command, data), ensure_ascii=False, indent=indent),
            flush=True,
        )
        return 0
    except workflow_common.WorkflowError as error:
        print(
            json.dumps(response(command, error.data, error), ensure_ascii=False, indent=indent),
            flush=True,
        )
    except ValueError as error:
        print(
            json.dumps(
                response(
                    command,
                    error=workflow_common.WorkflowError("INVALID_ARGUMENT", str(error)),
                ),
                ensure_ascii=False,
                indent=indent,
            ),
            flush=True,
        )
    except Exception as error:
        print(
            json.dumps(
                response(
                    command,
                    error=workflow_common.WorkflowError("INTERNAL_ERROR", str(error)),
                ),
                ensure_ascii=False,
                indent=indent,
            ),
            flush=True,
        )
    return 2


if __name__ == "__main__":
    sys.exit(cli())
