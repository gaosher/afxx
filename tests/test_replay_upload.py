from afxx import config
from afxx.afx.fuzz_replay import validate_options


def test_upload_default_and_explicit_override(cfg):
    cfg.pop('upload')
    assert config.normalize_config(cfg)['upload']['enabled'] is True
    cfg['upload']={'enabled':False}
    assert config.normalize_config(cfg)['upload']['enabled'] is False
    validate_options(cfg,{'upload':True})
