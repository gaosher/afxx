"""Stdio MCP interface; CLI processes own configuration and workflow execution."""

import argparse
import asyncio
import json
import os
import signal
import uuid
import sys
from typing import Annotated, Literal
from pathlib import Path
import re
from pydantic import Field

from afxx import mcp_operations
from afxx.common import JOB_ID_PATTERN
from afxx.config import default_config_path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

Command = Literal[
    "env init",
    "env update",
    "env check",
    "deploy",
    "deploy list",
    "deploy show",
    "replay",
    "replay list",
    "replay show",
    "replay compare",
    "client status",
    "client start",
    "client stop",
    "client restart",
    "doctor",
    "status",
    "issue list",
    "issue cases",
    "platform me",
    "platform summary",
    "issue claim",
    "issue preview",
    "issue status",
    "issue wait",
    "issue verify",
    "issue verify-check",
    "issue renew",
    "issue release",
    "case list",
    "case spec",
    "case history",
    "retest list",
    "issue detail",
    "issue tasks",
    "issue regression-preview",
    "issue submit-summary",
    "case execution",
    "evidence list",
    "evidence read",
    "config init",
]


async def invoke(arguments):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "afxx.cli",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
        start_new_session=True,
    )
    try:
        stdout, _ = await process.communicate()
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
    return process.returncode, stdout.decode("utf-8", errors="replace")


LONG_COMMANDS = {"env init", "env update", "deploy",
                 "client start", "client stop", "client restart", "issue verify"}
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
JobId = Annotated[str, Field(pattern=re.compile(JOB_ID_PATTERN))]
Timeout = Annotated[float, Field(gt=0, le=3600)]


def envelope(command, code, message):
    return {"schema_version":1, "ok":False, "command":command, "data":None,
            "error":{"code":code,"message":message}}


def tool_result(payload, code=0):
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))],
                          structuredContent=payload, isError=code != 0 or not payload["ok"])


def prepare(command, arguments, config, default_config):
    """Resolve one config path; explicit conflicts are never silently overridden."""
    args = list(arguments or [])
    clean, paths = [], []
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--config":
            index += 1
            if index == len(args) or args[index].startswith("--"):
                raise ValueError("--config requires a path")
            paths.append(args[index])
        elif item.startswith("--config="):
            paths.append(item.split("=",1)[1])
        else:
            clean.append(item)
        index += 1
    if config is not None:
        paths.append(config)
    if any(not p.strip() for p in paths):
        raise ValueError("config path cannot be empty")
    paths = [str(Path(p).expanduser().resolve()) for p in paths]
    if len(set(paths)) > 1:
        raise ValueError("conflicting explicit config paths")
    if command == "config init":
        if paths:
            raise ValueError("config init uses --output, not config/--config")
        return clean, None
    path = paths[0] if paths else str(Path(default_config or default_config_path()).expanduser().resolve())
    if command in ("env init", "env update", "deploy") and "--detach" not in clean:
        clean.append("--detach")
    return ["--config", path, *clean], path


def create_server(default_config=None):
    server = FastMCP("AFXX", instructions=(
        "Prefer typed tools; afxx_run is for commands without a typed tool. env update accepts explicit --afx VERSION|latest, --torch-npu DATE|latest, --torch VERSION and --cann DATE|latest [--ops]; no selectors only reports installed versions. --ops requires --cann. Optional --save-config writes resolved versions to the submitted config on successful status completion. Read afxx_help before constructing unfamiliar CLI arguments. Long commands return a local "
        "invocation receipt: query afxx_operation_status with the returned request_id for the CLI result. env init/update allocate IDs automatically; env check returns directly without a receipt. Do not supply request_id for env commands. "
        "A completed invocation is not necessarily a completed remote job. Use afxx_job_status or "
        "afxx_issue_status next. Replay returns request_id/run directly; query replay show, not operation_status. Disconnecting does not cancel a submitted invocation. "
        "Restart and verify remain separate explicit actions."))

    async def dispatch(command, arguments=None, config=None, request_id=None, request_key=None):
        keyed = command in ("issue release", "issue submit-summary")
        arguments = list(arguments or [])
        if keyed and (request_id is not None or any(a == "--request-id" or a.startswith("--request-id=") for a in arguments)):
            return tool_result(envelope(command, "INVALID_ARGUMENT", "use request_key/--request-key instead of request_id"))
        if request_key is not None:
            if not keyed:
                return tool_result(envelope(command, "INVALID_ARGUMENT", "request_key only applies to issue release/submit-summary"))
            keys = [arguments[i+1] if i+1 < len(arguments) else "" for i,a in enumerate(arguments) if a == "--request-key"]
            keys += [a.split("=", 1)[1] for a in arguments if a.startswith("--request-key=")]
            if any(key != request_key for key in keys):
                return tool_result(envelope(command, "REQUEST_CONFLICT", "tool and CLI request_key differ"))
            if not keys:
                arguments += ["--request-key", request_key]
        environment_command = command in ("env init", "env update", "env check")
        if environment_command:
            if request_id is not None or any(arg == "--request-id" or arg.startswith("--request-id=") for arg in (arguments or [])):
                return tool_result(envelope(command, "INVALID_ARGUMENT", "env commands do not accept request_id; init/update allocate IDs and check returns directly"))
        try:
            args, path = prepare(command, arguments, config, default_config)
        except ValueError as error:
            return tool_result(envelope(command, "CONFIG_CONFLICT", str(error)))
        if command in LONG_COMMANDS and not environment_command and (request_id is None or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", request_id)):
            return tool_result(envelope(command, "INVALID_ARGUMENT", "long MCP commands require request_id for recovery"))
        grouped_verify = command == "issue verify" and not any(
            arg in ("--job-id", "--job") or arg.startswith(("--job-id=", "--job=")) for arg in args)
        forwarded = {"deploy", "deploy list", "deploy show",
                     "replay", "replay list", "replay show", "replay compare", "status"}
        if (command in forwarded or grouped_verify) and request_id is not None:
            keys = [args[i + 1] if i + 1 < len(args) else "" for i, arg in enumerate(args) if arg == "--request-id"]
            keys += [arg.split("=", 1)[1] for arg in args if arg.startswith("--request-id=")]
            if keys and any(key != request_id for key in keys):
                return tool_result(envelope(command, "REQUEST_CONFLICT", "tool and CLI request_id differ"))
            if not keys:
                args += ["--request-id", request_id]
        if command in LONG_COMMANDS:
            try:
                operation_id = uuid.uuid4().hex if command == "deploy" or grouped_verify else request_id
                payload = mcp_operations.submit(command, [*command.split(), *args], path, operation_id)
                if command == "deploy" or grouped_verify:
                    payload["data"]["group_request_id"] = request_id
            except (OSError, ValueError) as error:
                return tool_result(envelope(command,"OPERATION_STORAGE_ERROR",str(error)))
            return tool_result(payload)
        code, output = await invoke([*command.split(), *args])
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
                raise ValueError("invalid CLI envelope")
        except (ValueError, TypeError):
            payload = envelope(command,"CLI_PROTOCOL_ERROR", "CLI returned no valid workflow envelope")
        return tool_result(payload,code)

    @server.tool()
    async def afxx_help(command: Command | Literal["issue", "config", "evidence", "client", "case", "platform", "retest"] | None = None) -> str:
        """Read CLI help without loading config or contacting the NPU."""
        _, output = await invoke((command.split() if command else []) + ["--help"])
        return output

    @server.tool()
    async def afxx_run(command: Command, arguments: list[str] | None = None,
                       config: str | None = None, request_id: RequestId | None = None,
                       request_key: RequestId | None = None) -> CallToolResult:
        """Fallback for commands without a dedicated typed tool; use afxx_help for arguments.
        release/submit-summary accept optional request_key, never request_id.
        Arguments are tokens, never shell code. Short commands return the CLI envelope directly.
        Long commands: env init/update, deploy, client start/stop/restart, issue verify.
        Long commands return an invocation receipt; query afxx_operation_status.
        env init/update allocate IDs automatically; env check returns the result directly. All env commands reject request_id; other long commands require it.
        Conflicting explicit config paths are rejected; server config is only a default.
        """
        return await dispatch(command, arguments, config, request_id, request_key)

    @server.tool()
    async def afxx_operation_status(request_id: RequestId) -> CallToolResult:
        """Read durable local invocation state, including the original CLI result when completed.
        No cancellation or resubmission. Works after MCP reconnect on this machine/account.
        Completion describes the CLI call; use its job_id/round_no to track remote execution.
        """
        return tool_result(mcp_operations.status(request_id))

    @server.tool()
    async def afxx_deploy(request_id: RequestId,
                          config: str | None = None, dry_run: bool = False, coordinate_client: bool = False) -> CallToolResult:
        """Create a new deploy run in request_id on every call. Return a local invocation receipt.
        Query operation_status with its ID, then deploy list/show with the group request_id.
        Deployment records do not select the package used by replay or verify.
        """
        args = ["--dry-run"] if dry_run else []
        if coordinate_client:
            args += ["--coordinate-client"]
        return await dispatch("deploy",args,config,request_id)

    @server.tool()
    async def afxx_replay(request_id: RequestId, seed: str | None = None,
                         seeds_file: str | None = None, dump: bool = False,
                         config: str | None = None) -> CallToolResult:
        """Run afx fuzz replay using the current AFX queue; specify exactly one of seed or local seeds_file.
        Every call starts a new AFXX run in request_id, without a deployment reference.
        Uses the configured Python/CANN environment and replay devices/compile threads. dump=True enables task diagnostics; results are always uploaded.
        Query with afxx_run replay list/show/compare and --request-id, optionally --run/--runs.
        Returns request_id, run and submission state; not an MCP operation receipt.
        """
        if (seed is None) == (seeds_file is None):
            return tool_result(envelope("replay","INVALID_ARGUMENT","provide exactly one of seed or seeds_file"))
        args = (["--seed",seed] if seed is not None else ["--seeds-file",str(Path(seeds_file).expanduser().resolve())])
        if dump: args += ["--dump"]
        return await dispatch("replay",args,config,request_id)

    @server.tool()
    async def afxx_job_status(request_id: JobId, config: str | None = None,
                              wait: bool = False, since: str | None = None,
                              timeout: Annotated[float, Field(gt=0, le=50)] | None = None,
                              attempt: str | None = None, fields: list[str] | None = None) -> CallToolResult:
        """Query job summary, one attempt (optional fields), or wait for changes.
        attempt and wait are exclusive; fields requires attempt; since/timeout require wait.
        Fields are nonempty dotted field names. Terminal replay may download evidence.
        """
        if ((since is not None or timeout is not None) and not wait) or (attempt is not None and wait) or (fields is not None and attempt is None):
            return tool_result(envelope("status", "INVALID_ARGUMENT", "attempt conflicts with wait; fields requires attempt; since/timeout require wait"))
        if attempt is not None and not attempt.strip():
            return tool_result(envelope("status", "INVALID_ARGUMENT", "attempt cannot be empty"))
        if fields is not None and (not fields or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", f) for f in fields)):
            return tool_result(envelope("status", "INVALID_ARGUMENT", "fields must be nonempty dotted field names"))
        args = ["--request-id", request_id]
        if wait: args += ["--wait", "--timeout", str(timeout if timeout is not None else 30)]
        if since is not None: args += ["--since", since]
        if attempt is not None: args += ["--attempt", attempt]
        if fields is not None: args += ["--fields", ",".join(fields)]
        return await dispatch("status", args, config)

    @server.tool()
    async def afxx_client(action: Literal["status", "start", "stop", "restart"],
                          request_id: RequestId | None = None, config: str | None = None,
                          timeout: Timeout | None = None) -> CallToolResult:
        """Manage the existing unique remote client.
        status returns directly (default timeout 30s), does not prove retest readiness,
        and rejects request_id. start/stop/restart require request_id and return a durable
        invocation receipt; query afxx_operation_status (default timeout 300s).
        Start may start fuzz. Stop/restart drain work; timeout does not force-kill.
        Restart reuses the same client and does not submit verify.
        """
        command = "client " + action
        if action == "status" and request_id is not None:
            return tool_result(envelope(command, "INVALID_ARGUMENT", "client status does not accept request_id"))
        if timeout is None:
            timeout = 30 if action == "status" else 300
        return await dispatch(command, ["--timeout", str(timeout)], config, request_id)

    @server.tool()
    async def afxx_issue(action: Literal["claim", "preview", "verify", "release", "verify-check"],
                         issue_id: Annotated[str, Field(pattern=r"^[FAP][0-9]{1,9}$")],
                         request_id: RequestId | None = None, deploy_job_id: JobId | None = None,
                         request_key: RequestId | None = None,
                         round_no: Annotated[int, Field(ge=1, le=999999)] | None = None,
                         wait: bool | None = None, timeout: Annotated[float, Field(gt=0, le=50)] | None = None,
                         full: bool | None = None, config: str | None = None) -> CallToolResult:
        """Act on an explicit issue_id (required for every action).
        claim requires request_id; release accepts optional request_key (auto-generated by default). Both return directly.
        preview is read-only, accepts only full (optional), and returns directly.
        verify-check requires round_no; optional wait/timeout (<=50s) polls read-only,
        returns state/progress/failures directly, never renews or restarts. timed_out is not failure.
        verify requires the repair request_id, probes the currently installed package and
        returns an invocation receipt: query afxx_operation_status, then afxx_issue_status.
        Restart the client separately before verification; this tool never restarts it.
        For verify request_id identifies the repair group. deploy_job_id is a legacy record reference; omit it.
        Parameters irrelevant to the action are rejected, even full=false outside preview.
        """
        command = "issue " + action
        supplied = {key for key, value in {"request_key": request_key, "request_id": request_id, "deploy_job_id": deploy_job_id, "full": full, "round_no": round_no, "wait": wait, "timeout": timeout}.items() if value is not None}
        allowed = {"claim": {"request_id"}, "preview": {"full"},
                   "verify": {"request_id", "deploy_job_id"}, "release": {"request_key"}, "verify-check": {"round_no", "wait", "timeout"}}[action]
        required = {"request_id"} if action == "verify" else {"round_no"} if action == "verify-check" else set() if action in ("preview", "release") else allowed
        if supplied - allowed or required - supplied:
            return tool_result(envelope(command, "INVALID_ARGUMENT",
                f"{action}: missing parameters {sorted(required - supplied)}; irrelevant parameters {sorted(supplied - allowed)}"))
        args = ["--issue-id", issue_id]
        if action == "verify-check":
            if timeout is not None and wait is not True:
                return tool_result(envelope(command, "INVALID_ARGUMENT", "timeout requires wait=true"))
            args += ["--round-no", str(round_no)]
            if wait: args += ["--wait"]
            if timeout is not None: args += ["--timeout", str(timeout)]
            return await dispatch(command, args, config)
        if action == "verify":
            return await dispatch(command, args + (["--job-id", deploy_job_id] if deploy_job_id else ["--request-id", request_id]), config, request_id)
        if action == "release":
            return await dispatch(command, args, config, request_key=request_key)
        if action == "claim":
            args += ["--request-id", request_id]
        elif full:
            args += ["--full"]
        return await dispatch(command, args, config)

    @server.tool()
    async def afxx_issue_status(issue_id: Annotated[str, Field(pattern=r"^[FAP][0-9]{1,9}$")],
                                round_no: Annotated[int, Field(ge=1, le=999999)],
                                include_tasks: bool = False, config: str | None = None) -> CallToolResult:
        """Read verify/regression summary; optionally include tasks for both associated rounds.
        round_no identifies the verify round. Pending is not failure. With include_tasks,
        verification.tasks and regression.tasks contain their respective tasks; no lease needed.
        """
        args = ["--issue-id", issue_id, "--round-no", str(round_no)]
        if include_tasks: args += ["--include-tasks"]
        return await dispatch("issue status", args, config)

    @server.tool()
    async def afxx_case_execution(exec_uuid: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")],
                                   config: str | None = None) -> CallToolResult:
        """Read platform execution details/logs by exec_uuid, not numeric exec_id.
        Preserves log_missing/log_truncated; does not contact the NPU or require an issue lease.
        """
        return await dispatch("case execution", ["--exec-uuid", exec_uuid], config)

    @server.tool()
    async def afxx_evidence_list(request_id: RequestId, offset: Annotated[int, Field(ge=0)] = 0,
                                 run: Annotated[int, Field(ge=1)] | None = None,
                                 config: str | None = None) -> CallToolResult:
        """List remote replay evidence and failure associations. offset is a file-list cursor.
        Select by request_id/run; omitted run means latest. Pin the returned run for pagination.
        Requires the configured NPU connection; does not start replay or generate missing dumps.
        """
        args = ["--request-id", request_id, "--offset", str(offset)]
        if run is not None: args += ["--run", str(run)]
        return await dispatch("evidence list", args, config)

    @server.tool()
    async def afxx_evidence_read(request_id: RequestId, path: str,
                                 offset: Annotated[int, Field(ge=0)] = 0,
                                 limit: Annotated[int, Field(ge=1, le=65536)] = 8192,
                                 sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None,
                                 run: Annotated[int, Field(ge=1)] | None = None,
                                 config: str | None = None) -> CallToolResult:
        """Read a remote artifact from evidence list in byte-sized pages.
        Select by request_id/run; omitted run means latest. Reuse the run returned by evidence list.
        path is the absolute remote path, never a local path. Reuse sha256 from a previous
        read to reject changed content. CLI enforces allowed evidence roots and symlink restrictions.
        """
        if not path.startswith("/") or ".." in path.split("/"):
            return tool_result(envelope("evidence read", "INVALID_ARGUMENT", "path must be absolute without parent traversal"))
        args = ["--request-id", request_id, "--path", path, "--offset", str(offset), "--limit", str(limit)]
        if run is not None: args += ["--run", str(run)]
        if sha256 is not None:
            args += ["--sha256", sha256]
        return await dispatch("evidence read", args, config)

    @server.tool()
    async def afxx_case(action: Literal["spec", "history"], case_key: str,
                        after: Annotated[int, Field(ge=0)] | None = None,
                        limit: Annotated[int, Field(ge=1, le=200)] | None = None,
                        config: str | None = None) -> CallToolResult:
        """Read a case's saved graph spec or execution history. Read-only, no NPU required.
        spec returns graph metadata (empty object if absent) and rejects pagination parameters.
        history defaults to 50 executions; pass next_cursor as after for the next page.
        """
        command = "case " + action
        if action == "spec" and (after is not None or limit is not None):
            return tool_result(envelope(command, "INVALID_ARGUMENT", "spec does not accept after/limit"))
        args = ["--case-key", case_key]
        if action == "history":
            args += ["--limit", str(limit if limit is not None else 50)]
            if after is not None: args += ["--after", str(after)]
        return await dispatch(command, args, config)

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="afxx mcp", description=__doc__)
    parser.add_argument("--config", help="default workflow configuration path")
    args = parser.parse_args(argv)
    create_server(args.config).run(transport="stdio")


if __name__ == "__main__":
    main()
