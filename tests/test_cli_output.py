"""CLI formatting must preserve machine-readable success and error responses."""
import io
import json

import pytest

import afxx.cli as cli
from afxx.common import WorkflowError


@pytest.mark.parametrize('terminal', [True, False])
@pytest.mark.parametrize('command', ['status', 'issue-preview', 'issue-claim'])
@pytest.mark.parametrize('error', [None, WorkflowError('TEST_FAILED', '测试失败'),
                                   ValueError('参数错误'), RuntimeError('内部错误')])
def test_json_output(monkeypatch, terminal, command, error):
    output = io.StringIO()
    monkeypatch.setattr(output, 'isatty', lambda: terminal)
    monkeypatch.setattr(cli.sys, 'stdout', output)
    monkeypatch.setattr(cli.sys, 'argv', ['afxx', command])

    def main():
        if error is not None:
            raise error
        return {'state': 'idle', 'message': '就绪'}

    monkeypatch.setattr(cli, 'main', main)
    assert cli.cli() == (2 if error else 0)
    text = output.getvalue()
    result = json.loads(text)
    assert result['ok'] is (error is None)
    assert text == json.dumps(result, ensure_ascii=False, indent=2 if terminal else None) + '\n'
    if error is None:
        assert result['data'] == {'state': 'idle', 'message': '就绪'}
    else:
        assert result['error']['message'] == str(error)
