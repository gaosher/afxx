import json
import sys
from pathlib import Path
import yaml
import pytest
from afxx import common as w, config, jobs, cli
from afxx.afx import platform as p


def filled_summary(issue_id="F152"):
    """Fill the shipped template with synthetic review evidence, not live results."""
    template = Path(__file__).parents[1] / "skills/afxxsummary/assets/summary.yaml"
    fields = config.read_config(template)
    fields.update(
        kind="bugfix_function", title="修复空维度的边界处理",
        symptom="Shape 含空维度时预期保留空输出，实际编译失败。",
        root_cause="边界计算未处理零长度，将空维度送入非空公式。",
        solution="修正边界，保留非空分支语义；新增空维度测试。",
        fix_ref="a" * 40,
        body_md=f"""## 问题与复现条件
- 复现：固定输入 shape=[0, 8]；原命令 `pytest -q tests/test_empty.py`。
- 基线：{'b' * 40}。

## 根因与修复分析
### 空维度进入非空公式
以下 IR 是契约测试的合成数据，不是线上现场。
```mlir
%extent = arith.constant 0 : index
```
- 错误现场：平台 https://platform.example，issue {issue_id}，执行 exec-0001 的 compile 日志，错误为 "extent: 0"。
- 错误机制：基线 src/bounds.cc 的 compute_extent 分支将空维度送入非空公式。
- 因果链：空维度 → 非空公式 → 无效 extent → 编译失败。

- 修复行为：零维度走空输出分支；未提供实测修复后 IR。
**验证依据**
- 原用例：相同输入/判定，修复前 compile_fail，修复后 pass；证据 exec-0001 与 exec-0002。
- 本地测试：`pytest -q tests/test_empty.py`，2 passed，覆盖空/非空分支。
- 未验证范围：其他 dtype 未覆盖。

## 人工检视重点
- 检视 fix_ref 中 src/bounds.cc::compute_extent，确认新增判断仅影响空维度。

## 同类问题定位指引
- 识别信号：输入含 0 维度并报告 extent 错误。
- 先比较分支前后的 IR；若输入无空维度，不直接套用本结论。

平台索引：https://platform.example · {issue_id} · verify #1 passed · 平台未选择回归候选。
""",
    )
    return fields


@pytest.fixture
def summary(platform_cfg, tmp_path):
    path = tmp_path / "summary.yaml"
    path.write_text(yaml.safe_dump(filled_summary(), allow_unicode=True, sort_keys=False))
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
    assert bodies[0]["knowledge"] == filled_summary()
    summary.write_text(summary.read_text().replace("其他 dtype 未覆盖", "新增 dtype 已覆盖"))
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


def test_summary_cli_preserves_errors_without_npu(platform_cfg, summary, monkeypatch, capsys):
    code = "PLATFORM_LEASE_LOST"
    monkeypatch.setattr(sys, "argv", ["afxx", "issue", "summarize", "--issue-id", "F152", "--summary-file", str(summary), "--request-id", "summary-1"])
    monkeypatch.setattr(config, "config", lambda _: platform_cfg)
    monkeypatch.setattr(jobs, "local_preflight", lambda *a: pytest.fail("no NPU connection"))
    def fail(*a):
        raise w.WorkflowError(code, "platform rejected")
    monkeypatch.setattr(p, "call", fail)
    assert cli.cli() == 2
    out = json.loads(capsys.readouterr().out)
    assert out["command"] == "issue summarize" and out["error"]["code"] == code


def test_summary_requires_explicit_request_id(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["afxx", "issue", "summarize", "--issue-id", "F152", "--summary-file", "summary.yaml"])
    assert cli.cli() == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "INVALID_ARGUMENT"


def test_summary_rejects_wrong_receipt(platform_cfg, summary, monkeypatch):
    monkeypatch.setattr(p, "call", lambda *a: dict(issue_id="F153", status="resolved", knowledge_id="K42"))
    with pytest.raises(w.WorkflowError) as error:
        p.summarize(platform_cfg, "F152", summary, "summary-1")
    assert error.value.code == "PLATFORM_RESPONSE_INVALID"
