#!/usr/bin/env python3
# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
"""CLI entry point for AF development workflows."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from af_forge.afx import platform
from af_forge.afx import replay as replay_adapter
from af_forge import common as workflow_common
from af_forge import config as workflow_config
from af_forge import jobs as workflow_jobs
from af_forge.environment import runtime as workflow_runtime


class AgentParser(argparse.ArgumentParser):
    def error(self, message):
        raise workflow_common.WorkflowError("INVALID_ARGUMENT", message)


def response(command, data=None, error=None):
    if command in ("init", "update") and data and "job" in data:
        status = data.get("status") or {}
        summary = {
            key: data[key] for key in ("job", "request_id", "state") if key in data
        }
        if data.get("dry_run"):
            summary.update(dry_run=True, local=data["local"])
        if data.get("reused"):
            summary["reused"] = True
        for key in ("phase", "artifact", "log", "error_code", "error"):
            if key in status:
                summary[key] = status[key]
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
            data["job_id"] = data["job"]
    return {
        "schema_version": 1,
        "ok": error is None,
        "command": command.replace("-", " ", 1) if command and command.startswith(("issue-", "execution-", "config-")) else command,
        "data": data,
        "error": None if error is None else {"code": error.code, "message": str(error)},
    }


def parse_arguments():
    parser = AgentParser(
        prog="af-forge", usage="%(prog)s <命令> [参数]",
        description="AF Forge · 环境管理、源码部署与 AFX 验证", allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, title="命令", metavar="", prog="af-forge")
    groups = {}
    env_jobs = (
        "init",
        "update",
        "check",
    )
    jobs = (
        "deploy",
        "replay",
    )
    issues = (
        "issue-claim",
        "issue-preview",
        "issue-status",
        "issue-wait",
        "issue-verify",
        "issue-renew",
        "issue-detail",
        "issue-tasks",
        "issue-regression-preview",
        "issue-summarize",
    )
    descriptions = {
        "init": "远端环境：初始化 Python 和 CANN 环境",
        "update": "远端环境：按配置更新 Python 依赖，默认不更新 CANN",
        "check": "远端环境：检查运行环境及 Python 依赖",
        "deploy": "远端工作流：同步本地源码，编包并安装 AF",
        "replay": "远端工作流：使用已部署的 AF 复跑指定 seed",
        "status": "远端工作流：查询 workflow 任务状态",
        "issue-claim": "问题验证：从 AFX 平台领取待修复问题",
        "issue-preview": "问题验证：查看复现用例和失败摘要",
        "issue-status": "问题验证：查询平台验证和回归结果",
        "issue-wait": "问题验证：轮询等待验证和回归结果",
        "issue-verify": "问题验证：针对已部署的 AF 提交验证",
        "issue-renew": "问题验证：续期已领取问题的租约",
        "issue-detail": "问题查询：查看生命周期、租约和历史轮次",
        "issue-tasks": "问题查询：查看指定轮次的任务与失败信息",
        "issue-regression-preview": "问题查询：预览平台回归候选集",
        "issue-summarize": "问题归档：提交已检视的本地修复总结",
        "execution-show": "执行查询：查看执行详情、Shape 和日志",
        "doctor": "本地检查：检查配置、连接工具及登录状态",
    }
    for name in (*env_jobs, *jobs, *issues, "execution-show", "status", "doctor"):
        parent = commands
        leaf = name
        if name in env_jobs or name.startswith(("issue-", "execution-", "config-")):
            group = "env" if name in env_jobs else name.split("-", 1)[0]
            leaf = name if group == "env" else name.split("-", 1)[1]
            if group not in groups:
                summary = {
                    "env": "管理远端 Python、CANN 和依赖环境",
                    "issue": "领取问题、提交验证及查询回归结果",
                    "execution": "查询平台执行详情和日志",
                }[group]
                group_parser = commands.add_parser(
                    group, help=summary, description=summary,
                    usage=f"%(prog)s <操作> [参数]", allow_abbrev=False,
                )
                groups[group] = group_parser.add_subparsers(
                    dest="action", required=True, title="操作", metavar="", prog=f"af-forge {group}",
                )
            parent = groups[group]
        cmd = parent.add_parser(leaf, help=descriptions[name], description=descriptions[name], allow_abbrev=False)
        cmd.set_defaults(command=name)
        cmd.add_argument(
            "--config",
            required=name in ("init", "update"),
            default=None if name in ("init", "update") else workflow_config.default_config_path(),
        )
        if name in (*env_jobs, *jobs) or name == "doctor":
            cmd.add_argument("--dry-run", action="store_true")
        if name in (*env_jobs, *jobs) or name in ("issue-claim", "issue-summarize"):
            cmd.add_argument("--request-id", required=name == "issue-summarize")
        if name in ("init", "update"):
            cmd.add_argument("--detach", action="store_true")
        if name == "issue-preview":
            cmd.add_argument(
                "--full", action="store_true", help="show full preview details"
            )
        if name == "deploy" or name in issues:
            cmd.add_argument("--issue-id", required=name not in ("deploy", "issue-verify", "issue-claim"),
                             help="AFX platform issue, e.g. F152")
        if name in ("issue-wait", "issue-status"):
            cmd.add_argument("--round-no", type=int, required=True, help="verification round returned by issue verify")
        if name == "issue-summarize":
            cmd.add_argument("--summary-file", required=True, help="reviewed local YAML knowledge fields")
        if name == "issue-tasks":
            cmd.add_argument("--round-no", type=int, required=True, help="verify or regression round number")
        if name == "execution-show":
            cmd.add_argument("--exec-uuid", required=True, help="AFX execution UUID, not numeric exec_id")
        if name == "issue-wait":
            cmd.add_argument("--timeout", type=float, default=3600)
        if name in ("status", "issue-verify"):
            cmd.add_argument("--job-id", "--job", dest="job", required=True)
        if name == "replay":
            reference = cmd.add_mutually_exclusive_group(required=True)
            reference.add_argument(
                "--job-id", "--job", dest="job", help="deploy job ID"
            )
            reference.add_argument(
                "--deployment", help="absolute remote deployment.json path"
            )
            seeds = cmd.add_mutually_exclusive_group(required=True)
            seeds.add_argument("--seed")
            seeds.add_argument("--seeds-file")
            uploads = cmd.add_mutually_exclusive_group()
            uploads.add_argument("--upload", action="store_true", default=None)
            uploads.add_argument("--no-upload", dest="upload", action="store_false")
    config_parser = commands.add_parser(
        "config", help="本地配置：生成和管理配置", description="本地配置：生成和管理配置",
        usage="%(prog)s <操作> [参数]", allow_abbrev=False,
    )
    config_commands = config_parser.add_subparsers(
        dest="action", required=True, title="操作", metavar="", prog="af-forge config",
    )
    template = config_commands.add_parser("init", help="生成带注释的配置模板", allow_abbrev=False)
    template.set_defaults(command="config-init")
    template.add_argument("--source", required=True, help="local graph-autofusion repository")
    template.add_argument("--output", default="af-forge-config.yaml")
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
    if args.command == "config-init":
        return workflow_config.write_template(args.source, args.output)
    if args.issue_id is not None:
        platform.validate_issue_id(args.issue_id)
    if args.job is not None:
        workflow_common.require_code(
            re.fullmatch(r"[0-9a-f]{32}", args.job),
            "INVALID_ARGUMENT",
            "invalid job ID",
        )
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
    except (ValueError, OSError, TypeError) as error:
        raise workflow_common.WorkflowError("CONFIG_INVALID", str(error)) from error
    if args.command in ("init", "update"):
        workflow_common.require_code(
            "python_env" in c,
            "CONFIG_INVALID",
            "init/update requires python_env configuration",
        )
    if args.command == "execution-show":
        return platform.execution_detail(c, args.exec_uuid)
    if args.command.startswith("issue-"):
        workflow_config.platform_settings(c)
        if args.command == "issue-claim":
            request_id = args.request_id or uuid.uuid4().hex
            if args.request_id is None:
                print(f"issue claim --request-id {request_id}", file=sys.stderr, flush=True)
            return platform.claim(c, request_id, issue_id=args.issue_id)
        if args.command == "issue-summarize":
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
            return platform.poll(c, args.issue_id, args.round_no)
        workflow_jobs.local_preflight("status")
        directory = str(Path(c["remote"]["state_dir"]) / args.job)
        lease = platform.claimed(c, args.issue_id) if args.issue_id else None
        context = {key: lease[key] for key in ("id", "lease_id")} if lease else None
        code = "import sys,json; sys.path.insert(0,sys.argv[1]); from af_forge.afx import platform as p; print(json.dumps(p.retry_remote(sys.argv[1],json.loads(sys.argv[2]))))"
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
        "deploy" if args.command == "doctor" else args.command, args.dry_run
    )
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
    if args.command == "status":
        data = workflow_jobs.query_job(c, args.job)
        workflow_common.require_code(
            data["state"] != "not_found", "JOB_NOT_FOUND", "未找到任务：" + args.job
        )
        try:
            cleanup = workflow_jobs.cleanup_after_status(c, args.job, data)
        except (OSError, ValueError, KeyError) as error:
            cleanup = {"state": "failed", "error": str(error)}
        if cleanup is not None:
            data["local_snapshot_cleanup"] = cleanup
        return data
    values = []
    if args.command == "replay":
        if args.job:
            args.deployment = str(
                Path(c["remote"]["state_dir"]) / args.job / "deployment.json"
            )
        workflow_common.require(
            args.deployment and Path(args.deployment).is_absolute(),
            "replay requires remote --deployment",
        )
        values = replay_adapter.requested_seeds(args.seed, args.seeds_file)
    request = {
        "command": args.command,
        "seeds": values,
        "batch": bool(args.seeds_file),
        "deployment": args.deployment,
        "upload": args.upload,
    }
    if args.command == "deploy" and args.issue_id:
        lease = platform.claimed(c, args.issue_id)
        request["issue"] = {key: lease[key] for key in ("id", "lease_id")}
    data = workflow_jobs.submit(
        c, request, args.request_id or uuid.uuid4().hex, args.dry_run
    )
    if (
        args.command
        in ("init", "update")
        and not args.dry_run
        and not args.detach
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
    return data


def cli():
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command in ("issue", "execution", "config") and len(sys.argv) > 2 and not sys.argv[2].startswith("-"):
        command += "-" + sys.argv[2]
    if command in ("_worker", "_python_check"):
        return main()
    try:
        data = main()
        indent = (
            2
            if command == "issue-preview"
            or (command == "issue-claim" and sys.stdout.isatty())
            else None
        )
        print(
            json.dumps(response(command, data), ensure_ascii=False, indent=indent),
            flush=True,
        )
        return 0
    except workflow_common.WorkflowError as error:
        print(
            json.dumps(response(command, error.data, error), ensure_ascii=False),
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
            ),
            flush=True,
        )
    return 2


if __name__ == "__main__":
    sys.exit(cli())
