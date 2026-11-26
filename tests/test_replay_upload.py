import sys
import pytest
from afxx import cli, common, config
from afxx.afx.fuzz_replay import validate_options


@pytest.mark.parametrize('flag', ['--upload', '--no-upload'])
def test_upload_switches_removed(monkeypatch, capsys, flag):
    monkeypatch.setattr(sys, 'argv', ['afxx', 'replay', '--seed', 'seed', flag])
    assert cli.cli() == 2
    assert 'INVALID_ARGUMENT' in capsys.readouterr().out


def test_upload_configuration_removed(cfg):
    cfg['upload'] = {'enabled': False}
    with pytest.raises((ValueError, common.WorkflowError)):
        config.normalize_config(cfg)


@pytest.mark.parametrize('value', [False, True, None])
def test_upload_request_override_rejected(cfg, value):
    with pytest.raises(common.WorkflowError):
        validate_options(cfg, {'upload': value})
