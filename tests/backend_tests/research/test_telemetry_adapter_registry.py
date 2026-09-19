"""Task 09 09-C: the allowlisted adapter seam and its generic fallback.

These tests exercise the seam in isolation (no database, no HTTP):

* the registry is empty by default and only an explicit registration can
  resolve an adapter;
* an unknown id, an absent ref or a release version outside the declared range
  falls back to the untouched generic result;
* enrichment is additive: the generic event type, source and payload win on
  conflict and the adapter version is recorded as provenance;
* the Codex BYOA fixture is qualified only by conformance evidence bound to its
  exact artifact/adapter identity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research.study.agents.enums import DistributionMode, QualificationStatus
from research.study.agents.models import AdapterRef, AgentReleaseV1
from research.study.agents.registry import (
    byoa_identity_qualified,
    derive_qualification_status,
)
from research.telemetry.enums import CanonicalEventType, EventSource
from research.telemetry.normalization import (
    GENERIC_ACP_NORMALIZER_VERSION,
    AdapterSpec,
    clear_adapters_for_tests,
    normalize_acp_observation,
    register_adapter,
    registered_adapter_ids,
    resolve_adapter,
    unregister_adapter,
)
from research.telemetry.normalization.generic_acp import GenericAcpNormalizer

AGENTS_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "research" / "agents"
)

_SESSION_PROMPT: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "session/prompt",
    "params": {"sessionId": "acp-session-1"},
}


class _RecordingAdapter:
    """An allowlisted adapter that tries to rewrite a generic payload field."""

    adapter_version = "0.4.0"
    mapping_rule_version = "recording-v1"
    supported_release_ranges = [">=1.2.0,<1.3.0"]

    def __init__(self) -> None:
        self.enrich_calls = 0

    def enrich(self, candidate: Any) -> Any:
        self.enrich_calls += 1
        payload = dict(candidate.payload)
        # Additive vendor label plus an attempted overwrite of a generic field.
        payload["adapter_label"] = "codex"
        payload["session_id"] = "hijacked"
        return candidate.model_copy(update={"payload": payload})


class _SecretInjectingAdapter:
    """An allowlisted adapter that tries to smuggle a credential."""

    adapter_version = "0.4.0"
    mapping_rule_version = "secret-v1"
    supported_release_ranges = [">=1.2.0,<1.3.0"]

    def enrich(self, candidate: Any) -> Any:
        payload = dict(candidate.payload)
        payload["api_key"] = "sk-CANARY-ADAPTER-0001"
        payload["note"] = "ghp_CANARYADAPTER0002"
        return candidate.model_copy(update={"payload": payload})


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_adapters_for_tests()
    yield
    clear_adapters_for_tests()


def _register_default() -> None:
    register_adapter(
        AdapterSpec(
            adapter_id="acp-adapter",
            adapter_version="0.4.0",
            supported_release_ranges=(">=1.2.0,<1.3.0",),
        ),
        _RecordingAdapter,
    )


# ---------------------------------------------------------------------------
# Registry resolution
# ---------------------------------------------------------------------------


def test_registry_is_empty_by_default_and_unknown_ids_fall_back():
    assert registered_adapter_ids() == ()
    assert resolve_adapter(None) is None
    assert resolve_adapter("") is None
    assert resolve_adapter("does-not-exist") is None
    assert resolve_adapter(AdapterRef(adapter_id="unknown", version="1.0.0")) is None


def test_registration_is_explicit_and_duplicate_is_rejected():
    spec = AdapterSpec(adapter_id="acp-adapter", adapter_version="0.4.0")
    register_adapter(spec, _RecordingAdapter)
    assert registered_adapter_ids() == ("acp-adapter",)

    with pytest.raises(ValueError):
        register_adapter(spec, _RecordingAdapter)

    register_adapter(spec, _RecordingAdapter, replace=True)
    assert unregister_adapter("acp-adapter") is True
    assert unregister_adapter("acp-adapter") is False


def test_resolve_adapter_honours_the_declared_release_range():
    _register_default()

    assert resolve_adapter("acp-adapter", release_version="1.2.0") is not None
    assert resolve_adapter("acp-adapter", release_version="1.2.3") is not None
    # Outside the declared range -> generic fallback.
    assert resolve_adapter("acp-adapter", release_version="1.3.0") is None
    assert resolve_adapter("acp-adapter", release_version="2.0.0") is None


def test_resolve_adapter_uses_registered_ranges_only():
    """A caller-supplied ref may not widen (or narrow) the allowlisted window."""
    register_adapter(
        AdapterSpec(
            adapter_id="acp-adapter",
            adapter_version="0.4.0",
            supported_release_ranges=(">=1.0.0,<2.0.0",),
        ),
        _RecordingAdapter,
    )
    ref = AdapterRef(
        adapter_id="acp-adapter",
        version="0.4.0",
        supported_release_ranges=[">=2.0.0"],
    )

    # The ref's wider range is ignored: activation follows the registration.
    assert resolve_adapter(ref, release_version="1.2.3") is not None
    assert resolve_adapter(ref, release_version="2.1.0") is None


def test_a_registration_without_ranges_is_fail_closed_for_a_known_version():
    register_adapter(
        AdapterSpec(adapter_id="acp-adapter", adapter_version="0.4.0"),
        _RecordingAdapter,
    )

    # No declared window: the version cannot be proven compatible.
    assert resolve_adapter("acp-adapter", release_version="1.2.3") is None
    # Without a version there is nothing to prove, so the id still activates.
    assert resolve_adapter("acp-adapter") is not None


def test_unparseable_range_is_fail_closed():
    register_adapter(
        AdapterSpec(
            adapter_id="acp-adapter",
            adapter_version="0.4.0",
            supported_release_ranges=("not-a-range",),
        ),
        _RecordingAdapter,
    )

    assert resolve_adapter("acp-adapter", release_version="1.2.3") is None


def test_adapter_spec_rejects_blank_identity():
    with pytest.raises(ValueError):
        AdapterSpec(adapter_id="  ", adapter_version="0.4.0")
    with pytest.raises(ValueError):
        AdapterSpec(adapter_id="acp-adapter", adapter_version="")


# ---------------------------------------------------------------------------
# Generic fallback and enrichment
# ---------------------------------------------------------------------------


def test_generic_result_is_unchanged_without_an_adapter():
    result = normalize_acp_observation(_SESSION_PROMPT)
    baseline = GenericAcpNormalizer().normalize(_SESSION_PROMPT)

    assert result == baseline
    assert result.adapter_version is None
    assert result.candidates[0].adapter_version is None
    assert result.source == EventSource.ACP
    assert result.normalizer_version == GENERIC_ACP_NORMALIZER_VERSION


def test_unknown_adapter_returns_the_untouched_generic_result():
    result = normalize_acp_observation(
        _SESSION_PROMPT,
        adapter_ref="unknown-adapter",
        release_version="1.2.3",
    )
    baseline = GenericAcpNormalizer().normalize(_SESSION_PROMPT)

    assert result == baseline
    assert result.adapter_version is None


def test_enrichment_preserves_generic_fields_and_tags_provenance():
    _register_default()

    result = normalize_acp_observation(
        _SESSION_PROMPT,
        adapter_ref="acp-adapter",
        release_version="1.2.3",
    )
    candidate = result.candidates[0]

    # The generic mapping is authoritative and never rewritten.
    assert candidate.event_type == CanonicalEventType.AGENT_MESSAGE_STARTED
    assert candidate.mapping_rule_id == "acp.session.prompt"
    assert candidate.payload["session_id"] == "acp-session-1"
    # Adapter-injected fields are re-filtered: the deny-by-default policy
    # redacts the vendor label rather than letting it through untouched.
    assert candidate.payload["adapter_label"] == "[REDACTED]"
    assert candidate.adapter_version == "0.4.0"
    assert result.adapter_version == "0.4.0"
    assert result.normalizer_version == GENERIC_ACP_NORMALIZER_VERSION


def test_enrichment_cannot_smuggle_a_secret_through_the_adapter():
    """Adapter-injected secret material is dropped before it leaves the seam."""
    register_adapter(
        AdapterSpec(
            adapter_id="acp-adapter",
            adapter_version="0.4.0",
            supported_release_ranges=(">=1.2.0,<1.3.0",),
        ),
        _SecretInjectingAdapter,
    )

    result = normalize_acp_observation(
        _SESSION_PROMPT,
        adapter_ref="acp-adapter",
        release_version="1.2.3",
    )
    payload = result.candidates[0].payload

    assert "api_key" not in payload
    assert not any(
        "CANARY" in str(value) for value in payload.values()
    ), payload


def test_enrichment_does_not_mutate_the_generic_candidates():
    _register_default()
    baseline = GenericAcpNormalizer().normalize(_SESSION_PROMPT)

    normalize_acp_observation(
        _SESSION_PROMPT,
        adapter_ref="acp-adapter",
        release_version="1.2.3",
    )

    # The first, generic-only result is unaffected by a later enrichment pass.
    assert baseline.candidates[0].adapter_version is None
    assert "adapter_label" not in baseline.candidates[0].payload
    assert baseline.adapter_version is None


def test_every_candidate_of_a_multi_candidate_observation_is_enriched():
    _register_default()
    observation = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "tool-1",
                "status": "completed",
                "title": "read",
            }
        },
    }

    result = normalize_acp_observation(observation, adapter_ref="acp-adapter")

    assert len(result.candidates) == 2
    assert all(candidate.adapter_version == "0.4.0" for candidate in result.candidates)


# ---------------------------------------------------------------------------
# Codex BYOA fixture and qualification binding
# ---------------------------------------------------------------------------


def test_codex_byoa_fixture_derives_qualified_and_projects_adapter_identity():
    raw = json.loads(
        (AGENTS_FIXTURE_DIR / "release_codex_byoa_v1.json").read_text()
    )

    assert raw["distribution_mode"] == "BYOA_EXTERNAL"
    assert raw["agent_package"] == "codex"
    assert derive_qualification_status(raw) == QualificationStatus.QUALIFIED

    release = AgentReleaseV1.model_validate(
        {key: value for key, value in raw.items() if key != "conformance"}
    )
    assert release.distribution_mode == DistributionMode.BYOA_EXTERNAL
    assert release.is_byoa is True
    assert release.agent_package == "codex"
    assert release.adapter is not None
    assert release.adapter.adapter_id == "acp-adapter"
    assert release.adapter.version == "0.4.0"


def test_codex_byoa_fixture_is_unqualified_when_the_receipt_does_not_bind():
    raw = json.loads(
        (AGENTS_FIXTURE_DIR / "release_codex_byoa_v1.json").read_text()
    )
    receipt = dict(raw["conformance"][0])

    wrong_artifact = dict(receipt, artifact_digest="sha256:" + "9" * 64)
    assert (
        derive_qualification_status({**raw, "conformance": [wrong_artifact]})
        == QualificationStatus.UNQUALIFIED
    )

    wrong_adapter = dict(receipt, adapter_digest="sha256:" + "8" * 64)
    assert (
        derive_qualification_status({**raw, "conformance": [wrong_adapter]})
        == QualificationStatus.UNQUALIFIED
    )

    no_cases = dict(receipt, case_results=[])
    assert (
        derive_qualification_status({**raw, "conformance": [no_cases]})
        == QualificationStatus.UNQUALIFIED
    )


def test_codex_byoa_receipt_host_is_optional_and_never_cross_binds():
    """A BYOA receipt binds the release manifest digest; the host is optional."""
    raw = json.loads(
        (AGENTS_FIXTURE_DIR / "release_codex_byoa_v1.json").read_text()
    )
    receipt = dict(raw["conformance"][0])

    hostless = dict(receipt)
    hostless.pop("host")
    assert (
        derive_qualification_status({**raw, "conformance": [hostless]})
        == QualificationStatus.QUALIFIED
    )
    assert byoa_identity_qualified({**raw, "conformance": [hostless]}) is True

    # The same receipt can never qualify a *packaged* release whose artifact
    # digest differs, even when its platform matches a declared artifact.
    packaged = json.loads(
        (AGENTS_FIXTURE_DIR / "release_codex_acp_v1.json").read_text()
    )
    packaged["conformance"] = [dict(receipt, host={"os": packaged["artifacts"][0]["os"], "arch": packaged["artifacts"][0]["arch"]})]
    assert (
        derive_qualification_status(packaged) == QualificationStatus.UNQUALIFIED
    )
