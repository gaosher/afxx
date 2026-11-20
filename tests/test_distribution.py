"""Installed CLI configuration and actual MCP stdio protocol."""

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from af_forge import config


def test_config_fixed_home_ignores_cwd_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AF_FORGE_CONFIG", str(tmp_path / "other.yaml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other"))
    (tmp_path / "af-forge-config.yaml").touch()
    assert config.default_config_path() == tmp_path / ".config/af-forge/config.yaml"


def test_template_preserves_comments_and_does_not_overwrite(tmp_path):
    output = tmp_path / "af-forge-config.yaml"
    config.write_template(tmp_path, output)
    assert str(tmp_path) in output.read_text()
    assert "#" in output.read_text()
    with pytest.raises(FileExistsError):
        config.write_template(tmp_path, output)


def test_mcp_stdio(tmp_path):
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "af_forge.mcp_server"],
            env=dict(os.environ, HOME=str(tmp_path)),
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "af_forge_help",
                "af_forge_run",
            }
            help_result = await session.call_tool(
                "af_forge_help", {"command": "deploy"}
            )
            assert not help_result.isError
            assert "--request-id" in help_result.content[0].text
            group_help = await session.call_tool("af_forge_help", {"command": "issue"})
            assert "verify" in group_help.content[0].text
            leaf_help = await session.call_tool("af_forge_help", {"command": "issue verify"})
            assert "--job-id" in leaf_help.content[0].text
            grouped = await session.call_tool("af_forge_run", {
                "command": "issue claim", "config": str(tmp_path / "missing.yaml"),
            })
            assert grouped.isError
            assert grouped.structuredContent["command"] == "issue claim"
            assert grouped.structuredContent["error"]["code"] == "CONFIG_INVALID"
            for command in ("env init", "env update"):
                no_config = await session.call_tool("af_forge_run", {"command": command})
                assert no_config.isError
                assert no_config.structuredContent["error"]["code"] == "CONFIG_INVALID"
                explicit = await session.call_tool("af_forge_run", {
                    "command": command, "config": str(tmp_path / "missing.yaml"),
                })
                assert explicit.structuredContent["error"]["code"] == "CONFIG_INVALID"
            missing = await session.call_tool(
                "af_forge_run",
                {
                    "command": "doctor",
                    "config": str(tmp_path / "missing.yaml"),
                },
            )
            assert missing.isError
            assert missing.structuredContent["error"]["code"] == "CONFIG_INVALID"
            result = await session.call_tool(
                "af_forge_run",
                {
                    "command": "config init",
                    "arguments": [
                        "--source",
                        str(tmp_path),
                        "--output",
                        str(tmp_path / "af-forge-config.yaml"),
                    ],
                },
            )
            assert not result.isError
            assert result.structuredContent["ok"]
            status_help = await session.call_tool("af_forge_help", {"command": "status"})
            assert all(option in status_help.content[0].text for option in ("--attempt", "--fields", "--wait", "--since"))
            query_commands = {
                "issue detail": ["--issue-id", "F152"],
                "issue tasks": ["--issue-id", "F152", "--round-no", "2"],
                "issue regression-preview": ["--issue-id", "F152"],
                "execution show": ["--exec-uuid", "exec-0001"],
                "evidence list": ["--job-id", "a" * 32],
                "evidence read": ["--job-id", "a" * 32, "--path", "/tmp/a.mlir"],
            }
            for command, arguments in query_commands.items():
                help_result = await session.call_tool("af_forge_help", {"command": command})
                assert not help_result.isError and arguments[0] in help_result.content[0].text
                response = await session.call_tool("af_forge_run", {
                    "command": command, "arguments": arguments,
                    "config": str(tmp_path / "missing.yaml"),
                })
                assert response.isError
                assert response.structuredContent["command"] == command
                assert response.structuredContent["error"]["code"] == "CONFIG_INVALID"
            summary_help = await session.call_tool("af_forge_help", {"command": "issue summarize"})
            assert "--summary-file" in summary_help.content[0].text
            summary_missing_key = await session.call_tool("af_forge_run", {
                "command": "issue summarize", "arguments": ["--issue-id", "F152", "--summary-file", "summary.yaml"],
            })
            assert summary_missing_key.isError
            assert summary_missing_key.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
            rejected = await session.call_tool("af_forge_run", {"command": "_worker"})
            assert rejected.isError

    asyncio.run(check())


def test_help_matches_overview_without_loading_config(monkeypatch, capsys):
    from af_forge import cli

    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("help must not load config"))
    outputs = []
    for argument in ("--help", "help"):
        monkeypatch.setattr(sys, "argv", ["af-forge", argument])
        with pytest.raises(SystemExit) as result:
            cli.cli()
        assert result.value.code == 0
        outputs.append(capsys.readouterr().out)
    assert outputs[0] == outputs[1]
    assert "deploy" in outputs[1]
    assert "help" in outputs[1]

    import re

    overview = outputs[1]
    # Check every registered command, including future additions, has a visible summary.
    parser = cli.AgentParser
    captured = []
    original = parser.add_subparsers

    def capture(self, *args, **kwargs):
        action = original(self, *args, **kwargs)
        captured.append(action)
        return action

    monkeypatch.setattr(parser, "add_subparsers", capture)
    monkeypatch.setattr(sys, "argv", ["af-forge", "help"])
    with pytest.raises(SystemExit):
        cli.cli()
    for command in captured[0].choices:
        assert re.search(r"^\s+" + re.escape(command) + r"\s+[^\s].+$", overview, re.MULTILINE), command


@pytest.mark.parametrize("group,action,options", [
    ("issue", "claim", []), ("issue", "preview", ["--issue-id", "F152"]),
    ("issue", "renew", ["--issue-id", "F152"]),
    ("issue", "verify", ["--job-id", "a" * 32]),
    ("issue", "status", ["--issue-id", "F152", "--round-no", "1"]),
    ("issue", "wait", ["--issue-id", "F152", "--round-no", "1"]),
])
def test_grouped_commands_preserve_internal_operation(group, action, options, monkeypatch):
    from af_forge import cli
    monkeypatch.setattr(sys, "argv", ["af-forge", group, action, *options])
    args = cli.parse_arguments()
    assert args.command == f"{group}-{action}"
    assert cli.response(args.command)["command"] == f"{group} {action}"


@pytest.mark.parametrize("args", [["client", "start"], ["client", "stop"], ["client", "status"], ["client"], ["issue"], ["client-start"], ["issue-claim"], ["client", "claim"]])
def test_invalid_group_commands_do_not_load_config(args, monkeypatch, capsys):
    from af_forge import cli
    import json
    monkeypatch.setattr(sys, "argv", ["af-forge", *args])
    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("unexpected config read"))
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_config_init_uses_default_filename_without_loading_config(tmp_path, monkeypatch, capsys):
    from af_forge import cli
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["af-forge", "config", "init", "--source", str(tmp_path)])
    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("must not load config"))
    assert cli.cli() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "config init"
    assert result["data"]["config"] == str(tmp_path / ".config/af-forge/config.yaml")
    assert config.default_config_path() == tmp_path / ".config/af-forge/config.yaml"



@pytest.mark.parametrize("command", ["init", "update", "check"])
def test_env_default_and_explicit_config(tmp_path, monkeypatch, command):
    from af_forge import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["af-forge", "env", command])
    assert cli.parse_arguments().config == tmp_path / ".config/af-forge/config.yaml"
    monkeypatch.setattr(sys, "argv", ["af-forge", "env", command, "--config", "/chosen/config.yaml"])
    assert cli.parse_arguments().config == "/chosen/config.yaml"


def test_missing_default_config_guidance(tmp_path, monkeypatch, capsys):
    import json
    from af_forge import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["af-forge", "env", "init"])
    assert cli.cli() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "CONFIG_INVALID"
    assert "af-forge config init" in result["error"]["message"]
