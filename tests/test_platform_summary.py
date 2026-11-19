# -----------------------------------------------------------------------------------------------------------
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# This program is free software, you can redistribute it and/or modify it under the terms and conditions of
# CANN Open Software License Agreement Version 2.0 (the "License").
# Please refer to the License for details. You may not use this file except in compliance with the License.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND, EITHER EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT, MERCHANTABILITY, OR FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE in the root of the software repository for the full text of the License.
# -----------------------------------------------------------------------------------------------------------
import json
import sys
import pytest
from af_forge import common as w, config, jobs, cli
from af_forge.afx import platform as p


@pytest.fixture
def summary(platform_cfg, tmp_path):
    path = tmp_path / "summary.yaml"
    path.write_text("kind: bugfix_function\ntitle: 修复编译失败\nsymptom: 原始 seed 编译失败\nroot_cause: 错误边界\nsolution: 修正边界\nfix_ref: abc123\n")
    w.write(p.record_path(platform_cfg, "F152"), dict(
        id="F152", lease_id="lease", platform_url=platform_cfg["platform"]["url"], agent_id="agent-1"))
    return path


def test_summary_retry_preserves_request_and_receipt(platform_cfg, summary, monkeypatch):
    bodies = []
    response = dict(issue_id="F152", status="resolved", knowledge_id="K42")
    def api(c, method, path, body):
        assert method == "POST" and path == "/api/issues/F152/knowledge"
        bodies.append(json.loads(json.dumps(body)))
        if len(bodies) == 1:
            raise w.WorkflowError("PLATFORM_UNAVAILABLE", "response lost")
        return response
    monkeypatch.setattr(p, "call", api)
    with pytest.raises(w.WorkflowError):
        p.summarize(platform_cfg, "F152", summary, "summary-1")
    assert p.summarize(platform_cfg, "F152", summary, "summary-1") == response
    assert p.summarize(platform_cfg, "F152", summary, "summary-1") == response
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert bodies[0]["lease_id"] == "lease" and bodies[0]["request_key"] == "summary-1"
    assert bodies[0]["knowledge"]["fix_ref"] == "abc123"
    summary.write_text(summary.read_text().replace("修正边界", "不同修复"))
    with pytest.raises(w.WorkflowError) as error:
        p.summarize(platform_cfg, "F152", summary, "summary-1")
    assert error.value.code == "REQUEST_CONFLICT"
    assert len(bodies) == 2


@pytest.mark.parametrize("text", ["[]", "{}", "kind: x\nkind: y", "title: incomplete", "kind: x\ntitle: t\nsymptom: s\nroot_cause: r\nsolution: s\nunknown: x"])
def test_invalid_summary_does_not_post(platform_cfg, summary, monkeypatch, text):
    summary.write_text(text)
    monkeypatch.setattr(p, "call", lambda *a: pytest.fail("must not post"))
    with pytest.raises(w.WorkflowError) as error:
        p.summarize(platform_cfg, "F152", summary, "summary-1")
    assert error.value.code == "SUMMARY_INVALID"


@pytest.mark.parametrize("code", ["PLATFORM_WRONG_STATE", "PLATFORM_LEASE_LOST", "PLATFORM_UNAUTHORIZED"])
def test_summary_cli_preserves_errors_without_npu(platform_cfg, summary, monkeypatch, capsys, code):
    monkeypatch.setattr(sys, "argv", ["af-forge", "issue", "summarize", "--issue-id", "F152", "--summary-file", str(summary), "--request-id", "summary-1"])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("no NPU connection"))
    def fail(*a):
        raise w.WorkflowError(code, "platform rejected")
    monkeypatch.setattr(p, "call", fail)
    assert cli.cli() == 2
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "issue summarize" and out["error"]["code"] == code


def test_summary_requires_explicit_request_id(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["af-forge", "issue", "summarize", "--issue-id", "F152", "--summary-file", "summary.yaml"])
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_summary_rejects_wrong_receipt(platform_cfg, summary, monkeypatch):
    monkeypatch.setattr(p, "call", lambda *a: dict(issue_id="F153", status="resolved", knowledge_id="K42"))
    with pytest.raises(w.WorkflowError) as error:
        p.summarize(platform_cfg, "F152", summary, "summary-1")
    assert error.value.code == "PLATFORM_RESPONSE_INVALID"
