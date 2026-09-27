"""The bootstrap signing secret is resolved at request time (B-01).

The production compose used to omit ``BOOTSTRAP_SIGNING_SECRET`` and the router
read it once at import, before ``load_dotenv()`` ran: every bootstrap answered
503 and every heartbeat/upload was refused. The secret must now be honoured
when it is set after import and the session/telemetry routers must verify with
the same value the signer used.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.routers.research import bootstrap, sessions, telemetry
from research.runtime.bootstrap.service import BootstrapSigningContext


@pytest.fixture()
def _no_overrides():
    with patch.object(bootstrap, "_SIGNER", None), patch.object(
        bootstrap, "BOOTSTRAP_SIGNING_SECRET", None
    ), patch.object(sessions, "BOOTSTRAP_SIGNING_SECRET", None), patch.object(
        telemetry, "BOOTSTRAP_SIGNING_SECRET", None
    ):
        yield


def test_secret_set_after_import_is_used(monkeypatch, _no_overrides):
    monkeypatch.setenv("BOOTSTRAP_SIGNING_SECRET", "late-but-present")
    assert bootstrap.signing_secret() == "late-but-present"
    assert bootstrap._require_signer().secret == "late-but-present"
    assert sessions._signing_secret() == "late-but-present"
    assert telemetry._signing_secret() == "late-but-present"


def test_blank_secret_counts_as_missing(monkeypatch, _no_overrides):
    monkeypatch.setenv("BOOTSTRAP_SIGNING_SECRET", "   ")
    assert bootstrap.signing_secret() is None
    with pytest.raises(Exception) as refused:
        bootstrap._require_signer()
    assert refused.value.status_code == 503
    assert refused.value.detail["code"] == "SIGNING_SECRET_MISSING"
    # Verification with no secret must fail closed, never verify with "".
    assert sessions._signing_secret() == ""


def test_an_installed_signer_wins_over_the_environment(monkeypatch, _no_overrides):
    monkeypatch.setenv("BOOTSTRAP_SIGNING_SECRET", "env-secret")
    with patch.object(bootstrap, "_SIGNER", BootstrapSigningContext(secret="test-signer")):
        assert bootstrap.signing_secret() == "test-signer"
        assert bootstrap._require_signer().secret == "test-signer"
        assert telemetry._signing_secret() == "test-signer"


def test_module_override_wins_over_the_environment(monkeypatch, _no_overrides):
    monkeypatch.setenv("BOOTSTRAP_SIGNING_SECRET", "env-secret")
    with patch.object(bootstrap, "BOOTSTRAP_SIGNING_SECRET", "patched"):
        assert bootstrap.signing_secret() == "patched"
        assert sessions._signing_secret() == "patched"


def test_main_refuses_to_start_without_the_secret(monkeypatch, _no_overrides):
    import main

    monkeypatch.delenv("BOOTSTRAP_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("TEST_MODE", raising=False)
    with patch.object(main.uvicorn, "run") as run:
        with pytest.raises(SystemExit) as stopped:
            main.main()
        assert "BOOTSTRAP_SIGNING_SECRET" in str(stopped.value)
        run.assert_not_called()


def test_main_starts_under_test_mode_without_the_secret(monkeypatch, _no_overrides):
    import main

    monkeypatch.delenv("BOOTSTRAP_SIGNING_SECRET", raising=False)
    monkeypatch.setenv("TEST_MODE", "true")
    with patch.object(main.uvicorn, "run") as run:
        main.main()
        run.assert_called_once()
