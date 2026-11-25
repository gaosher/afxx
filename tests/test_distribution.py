"""Installed CLI configuration and actual MCP stdio protocol."""

import asyncio
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from afxx import config


def test_config_fixed_home_ignores_cwd_and_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AFXX_CONFIG", str(tmp_path / "other.yaml"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other"))
    (tmp_path / "afxx-config.yaml").touch()
    assert config.default_config_path() == tmp_path / ".config/afxx/config.yaml"


def test_template_preserves_comments_and_does_not_overwrite(tmp_path):
    output = tmp_path / "afxx-config.yaml"
    config.write_template(tmp_path, output)
    assert str(tmp_path) in output.read_text()
    assert "#" in output.read_text()
    import yaml
    platform = yaml.safe_load(output.read_text())["platform"]
    assert platform["url"] == "https://autofuse.online"
    assert platform["agent_id"]
    assert all(platform[k] is None for k in ("token_file", "remote_token_file", "client_token_file"))
    with pytest.raises(FileExistsError):
        config.write_template(tmp_path, output)


def test_mcp_stdio(tmp_path):
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "afxx.cli", "mcp"],
            env=dict(os.environ, HOME=str(tmp_path)),
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            assert {"afxx_help", "afxx_run", "afxx_deploy", "afxx_replay",
                    "afxx_client", "afxx_issue", "afxx_operation_status"} <= {
                tool.name for tool in listed.tools
            }
            help_result = await session.call_tool(
                "afxx_help", {"command": "deploy"}
            )
            assert not help_result.isError
            assert "--request-id" in help_result.content[0].text
            group_help = await session.call_tool("afxx_help", {"command": "issue"})
            assert "verify" in group_help.content[0].text
            fuzz_help = await session.call_tool("afxx_help", {"command": "fuzz run"})
            assert not fuzz_help.isError and "--count" in fuzz_help.content[0].text
            leaf_help = await session.call_tool("afxx_help", {"command": "issue verify"})
            assert "--job-id" in leaf_help.content[0].text
            grouped = await session.call_tool("afxx_run", {
                "command": "issue claim", "arguments": ["--issue-id", "F152"], "config": str(tmp_path / "missing.yaml"),
            })
            assert grouped.isError
            assert grouped.structuredContent["command"] == "issue claim"
            assert grouped.structuredContent["error"]["code"] == "CONFIG_INVALID"
            result = await session.call_tool(
                "afxx_run",
                {
                    "command": "config init",
                    "arguments": [
                        "--source",
                        str(tmp_path),
                        "--output",
                        str(tmp_path / "afxx-config.yaml"),
                    ],
                },
            )
            assert not result.isError
            assert result.structuredContent["ok"]
            rejected = await session.call_tool("afxx_run", {"command": "_worker"})
            assert rejected.isError

    asyncio.run(check())


def test_help_matches_overview_without_loading_config(monkeypatch, capsys):
    from afxx import cli

    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("help must not load config"))
    outputs = []
    for argument in ("--help", "help"):
        monkeypatch.setattr(sys, "argv", ["afxx", argument])
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
    monkeypatch.setattr(sys, "argv", ["afxx", "help"])
    with pytest.raises(SystemExit):
        cli.cli()
    for command in captured[0].choices:
        assert re.search(r"^\s+" + re.escape(command) + r"\s+[^\s].+$", overview, re.MULTILINE), command


@pytest.mark.parametrize("args", [["unknown-command"], ["issue"]])
def test_invalid_group_commands_do_not_load_config(args, monkeypatch, capsys):
    from afxx import cli
    import json
    monkeypatch.setattr(sys, "argv", ["afxx", *args])
    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("unexpected config read"))
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_config_init_uses_default_filename_without_loading_config(tmp_path, monkeypatch, capsys):
    from afxx import cli
    import json

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["afxx", "config", "init", "--source", str(tmp_path)])
    monkeypatch.setattr(config, "config", lambda *_: pytest.fail("must not load config"))
    assert cli.cli() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "config init"
    assert result["data"]["config"] == str(tmp_path / ".config/afxx/config.yaml")
    assert config.default_config_path() == tmp_path / ".config/afxx/config.yaml"



@pytest.mark.parametrize("command", ["init", "update", "check"])
def test_env_default_and_explicit_config(tmp_path, monkeypatch, command):
    from afxx import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["afxx", "env", command])
    assert cli.parse_arguments().config == tmp_path / ".config/afxx/config.yaml"
    monkeypatch.setattr(sys, "argv", ["afxx", "env", command, "--config", "/chosen/config.yaml"])
    assert cli.parse_arguments().config == "/chosen/config.yaml"


def test_missing_default_config_guidance(tmp_path, monkeypatch, capsys):
    import json
    from afxx import cli
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["afxx", "env", "init"])
    assert cli.cli() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "CONFIG_INVALID"
    assert "afxx config init" in result["error"]["message"]


def test_mcp_missing_dependency_guidance(monkeypatch, capsys):
    import builtins
    from afxx import cli

    original = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "afxx.mcp_server":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)
    monkeypatch.setattr(sys, "argv", ["afxx", "mcp"])
    assert cli.cli() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert "pip install 'afxx[mcp]'" in output.err
