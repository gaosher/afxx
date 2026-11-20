"""Stdio MCP interface; CLI processes own configuration and workflow execution."""

import argparse
import asyncio
import json
import os
import signal
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

Command = Literal[
    "env init",
    "env update",
    "env check",
    "deploy",
    "replay",
    "doctor",
    "status",
    "issue claim",
    "issue preview",
    "issue status",
    "issue wait",
    "issue verify",
    "issue renew",
    "issue detail",
    "issue tasks",
    "issue regression-preview",
    "issue summarize",
    "execution show",
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


def create_server(default_config=None):
    server = FastMCP(
        "AFXX",
        instructions=(
            "Use afxx_help for command options. Commands may change the NPU environment "
            "or platform state. init/update return a job ID; poll status. deploy and "
            "issue verify are separate actions. stderr carries diagnostic logs."
        ),
    )

    @server.tool()
    async def afxx_help(command: Command | Literal["issue", "execution", "config", "evidence"] | None = None) -> str:
        """Read CLI command options without loading config or contacting the NPU."""
        _, output = await invoke((command.split() if command else []) + ["--help"])
        return output

    @server.tool()
    async def afxx_run(
        command: Command,
        arguments: list[str] | None = None,
        config: str | None = None,
    ) -> CallToolResult:
        """Run an explicit workflow command, e.g. "issue preview" or "issue verify".
        arguments are CLI tokens, never a shell command.
        status returns compact progress; terminal replay automatically downloads complete evidence to /tmp.
        Terminal replies contain evidence_path and basic failures, not dump contents.
        replay --dump enables diagnostic dumps for that job without editing the config file.
        --attempt selects an execution, --fields selects dotted fields.
        --wait --since <cursor> returns changes/end or a tiny timeout receipt (default 30 seconds).

        init/update are detached; use status --job-id to poll. Public commands only.
        Init needs an existing local token file when platform is configured (no interactive prompt).
        Successful init status/wait also synchronizes credentials to the NPU.
        All commands default to ~/.config/afxx/config.yaml.
        Override via tool config, --config argument, or server --config.
        """
        arguments = list(arguments or [])
        path = config if config is not None else default_config
        if path is not None and command != "config init":
            arguments = ["--config", path, *arguments]
        if command in ("env init", "env update") and "--detach" not in arguments:
            arguments.append("--detach")
        code, output = await invoke([*command.split(), *arguments])
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict) or "ok" not in payload:
                raise ValueError("missing workflow envelope")
        except (ValueError, TypeError):
            payload = {
                "schema_version": 1,
                "ok": False,
                "command": command,
                "data": None,
                "error": {
                    "code": "CLI_PROTOCOL_ERROR",
                    "message": output or f"CLI exited {code}",
                },
            }
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))
            ],
            structuredContent=payload,
            isError=code != 0 or not payload["ok"],
        )

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(prog="afxx mcp", description=__doc__, allow_abbrev=False)
    parser.add_argument("--config", help="default workflow configuration path")
    args = parser.parse_args(argv)
    create_server(args.config).run(transport="stdio")


if __name__ == "__main__":
    main()
