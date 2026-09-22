"""D-1: the bootstrap manifest carries the enrollment's consent state.

The enrollment/consent recorded in the web UI is the single authority; the
server derives ``consent_active`` from the live enrollment and the client
mirrors it. These tests pin that derivation directly, without depending on the
wider bootstrap fixtures.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from research.participants.enums import EnrollmentStatus
from research.runtime.bootstrap.models import BootstrapTelemetryPolicy
from research.runtime.bootstrap.service import _policies

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _study() -> SimpleNamespace:
    return SimpleNamespace(
        research_config_json={
            "telemetry_policy": {
                "allowed_field_classes": ["SYSTEM", "BEHAVIORAL"],
                "content_capture": True,
            }
        }
    )


def _enrollment(status: EnrollmentStatus, consent_accepted_at):
    return SimpleNamespace(status=status, consent_accepted_at=consent_accepted_at)


def test_consent_active_is_true_for_an_active_consented_enrollment():
    policies = _policies(_study(), _enrollment(EnrollmentStatus.ACTIVE, NOW))

    assert policies.telemetry_policy is not None
    assert policies.telemetry_policy.consent_active is True
    # The study's own content flag is mirrored unchanged.
    assert policies.telemetry_policy.content_capture is True


def test_consent_active_is_false_without_an_accepted_consent():
    policies = _policies(_study(), _enrollment(EnrollmentStatus.ACTIVE, None))

    assert policies.telemetry_policy.consent_active is False


def test_consent_active_is_false_for_a_non_active_enrollment():
    policies = _policies(_study(), _enrollment(EnrollmentStatus.REVOKED, NOW))

    assert policies.telemetry_policy.consent_active is False


def test_consent_active_defaults_to_false_on_the_manifest_model():
    # An absent field means "not consented", never implied consent.
    assert BootstrapTelemetryPolicy().consent_active is False
