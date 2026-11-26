"""Independent fuzz is not an AFXX public interface."""
import asyncio
import sys
import pytest
from afxx import cli, common, jobs


@pytest.mark.parametrize("action", ["run", "status", "stop"])
def test_cli_rejects_independent_fuzz(monkeypatch, action):
    monkeypatch.setattr(sys, "argv", ["afxx", "fuzz", action])
    monkeypatch.setattr(cli.workflow_config, "config", lambda *a: pytest.fail("must reject before loading config"))
    with pytest.raises(common.WorkflowError) as error:
        cli.main()
    assert error.value.code == "INVALID_ARGUMENT"


def test_mcp_does_not_advertise_independent_fuzz():
    from afxx import mcp_server
    tools = asyncio.run(mcp_server.create_server().list_tools())
    for name in ("afxx_run", "afxx_help"):
        schema = next(tool.inputSchema for tool in tools if tool.name == name)
        import json
        assert '"fuzz run"' not in json.dumps(schema)
        assert '"fuzz status"' not in json.dumps(schema)
        assert '"fuzz stop"' not in json.dumps(schema)
    assert any(tool.name == "afxx_replay" for tool in tools)


def test_worker_rejects_removed_fuzz_command(cfg, tmp_path):
    job = tmp_path / "job"
    common.write(job / "config.yaml", cfg)
    common.write(job / "request.json", {"command": "fuzz-run"})
    assert jobs.worker(job) == 1
    assert common.read(job / "status.json")["error_code"] == "INVALID_ARGUMENT"
