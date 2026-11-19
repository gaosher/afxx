# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
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
    "init",
    "update",
    "deploy",
    "replay",
    "check",
    "doctor",
    "status",
    "client start",
    "client stop",
    "client status",
    "issue claim",
    "issue preview",
    "issue status",
    "issue wait",
    "issue verify",
    "issue renew",
    "config init",
]


async def invoke(arguments):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "af_forge.cli",
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
        "AF Forge",
        instructions=(
            "Use workflow_help for command options. Commands may change the NPU environment "
            "or platform state. init/update return a job ID; poll status. deploy and "
            "issue verify are separate actions. stderr carries diagnostic logs."
        ),
    )

    @server.tool()
    async def workflow_help(command: Command | Literal["client", "issue", "config"] | None = None) -> str:
        """Read CLI command options without loading config or contacting the NPU."""
        _, output = await invoke((command.split() if command else []) + ["--help"])
        return output

    @server.tool()
    async def workflow_run(
        command: Command,
        arguments: list[str] | None = None,
        config: str | None = None,
    ) -> CallToolResult:
        """Run an explicit workflow command, e.g. "client start" or "issue verify".
        arguments are CLI tokens, never a shell command.

        init/update are detached; use status --job-id to poll. Public commands only.
        Missing config uses AF_FORGE_CONFIG, cwd/af-forge-config.yaml, then the user config directory.
        """
        arguments = list(arguments or [])
        path = config if config is not None else default_config
        if path is not None and command != "config init":
            arguments = ["--config", path, *arguments]
        if command in ("init", "update") and "--detach" not in arguments:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="default workflow configuration path")
    args = parser.parse_args()
    create_server(args.config).run(transport="stdio")


if __name__ == "__main__":
    main()
