"""Managed replay never overrides the resident client's upload policy."""
import pytest
from afxx import common, replay_runs


@pytest.mark.parametrize("override,configured", [(True, False), (False, False), (None, True)])
def test_reject_upload_override_before_submission(cfg, monkeypatch, override, configured):
    cfg["upload"]["enabled"] = configured
    monkeypatch.setattr(replay_runs.jobs, "submit", lambda *a: pytest.fail("must not submit"))
    with pytest.raises(common.WorkflowError) as error:
        replay_runs.submit(cfg, {"seeds": ["seed"], "upload": override}, "group")
    assert error.value.code == "MANAGED_REPLAY_UNSUPPORTED"
