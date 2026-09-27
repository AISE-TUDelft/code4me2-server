"""Study telemetry policies authored in the study vocabulary resolve correctly.

The website offers STRUCTURAL / METRICS / DIAGNOSTICS / CODE_METADATA / CONTENT
(the study validator accepts both the study and the runtime names). Ingestion
resolves the frozen policy with ``PrivacyPolicy.from_study_policy``; before the
vocabulary mapping, a policy such as ``[STRUCTURAL, METRICS, DIAGNOSTICS,
CONTENT]`` resolved to ``{CONTENT}`` alone and every structural field
(tool kinds, stop reasons, decisions, statuses) was dropped at ingestion.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from research.participants.enums import EnrollmentStatus
from research.runtime.bootstrap.service import _policies
from research.telemetry.enums import FieldClass, PolicyAction
from research.telemetry.privacy.engine import PrivacyPolicy, filter_payload

EVERYTHING = {
    "allowed_field_classes": ["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA", "CONTENT"],
    "content_capture": True,
}


def _allowed(policy: PrivacyPolicy) -> set[FieldClass]:
    return set(policy.allowed_field_classes)


def test_empty_policy_keeps_the_metadata_default():
    policy = PrivacyPolicy.from_study_policy({}, consent_active=True)
    assert _allowed(policy) == {FieldClass.SYSTEM, FieldClass.BEHAVIORAL, FieldClass.CODE_METADATA}
    assert policy.content_allowed is False
    assert policy.code_metadata_mode == "allow"


def test_study_vocabulary_maps_to_runtime_classes():
    policy = PrivacyPolicy.from_study_policy(
        {"allowed_field_classes": ["STRUCTURAL", "METRICS", "DIAGNOSTICS"]}, consent_active=True
    )
    assert _allowed(policy) == {FieldClass.SYSTEM, FieldClass.BEHAVIORAL}
    # Code metadata was not declared, so it is hashed rather than stored.
    assert policy.code_metadata_mode == "hash"
    assert policy.action_for(FieldClass.CODE_METADATA) == PolicyAction.HASH


def test_everything_preset_keeps_structure_and_allows_content_with_consent():
    policy = PrivacyPolicy.from_study_policy(EVERYTHING, consent_active=True)
    assert _allowed(policy) == {
        FieldClass.SYSTEM,
        FieldClass.BEHAVIORAL,
        FieldClass.CODE_METADATA,
        FieldClass.CONTENT,
    }
    assert policy.content_allowed is True
    assert policy.action_for(FieldClass.CONTENT) == PolicyAction.ALLOW
    assert policy.action_for(FieldClass.SYSTEM) == PolicyAction.ALLOW
    assert policy.action_for(FieldClass.BEHAVIORAL) == PolicyAction.ALLOW


def test_content_is_redacted_without_consent_or_without_the_flag():
    no_consent = PrivacyPolicy.from_study_policy(EVERYTHING, consent_active=False)
    assert no_consent.action_for(FieldClass.CONTENT) == PolicyAction.REDACT
    no_flag = PrivacyPolicy.from_study_policy(
        {"allowed_field_classes": ["STRUCTURAL", "CONTENT"]}, consent_active=True
    )
    assert no_flag.content_allowed is False
    assert no_flag.action_for(FieldClass.CONTENT) == PolicyAction.REDACT
    assert FieldClass.SYSTEM in _allowed(no_flag)


def test_runtime_vocabulary_is_unchanged():
    policy = PrivacyPolicy.from_study_policy(
        {"allowed_field_classes": ["SYSTEM", "BEHAVIORAL"]}, consent_active=True
    )
    assert _allowed(policy) == {FieldClass.SYSTEM, FieldClass.BEHAVIORAL}


def test_unknown_names_are_ignored_and_secret_is_never_allowed():
    policy = PrivacyPolicy.from_study_policy(
        {"allowed_field_classes": ["NOT_A_CLASS", "SECRET", "METRICS"]}, consent_active=True
    )
    assert FieldClass.SECRET not in _allowed(policy)
    assert FieldClass.SYSTEM in _allowed(policy)


def test_structural_tool_fields_survive_ingestion_under_the_everything_preset():
    payload = {
        "tool_call_id": "t-1",
        "tool_kind": "edit",
        "status": "completed",
        "stop_reason": "end_turn",
        "decision": "allow",
    }
    policy = PrivacyPolicy.from_study_policy(EVERYTHING, consent_active=True)
    filtered, _summary = filter_payload(payload, policy)
    for key in ("tool_kind", "status", "stop_reason", "decision"):
        assert filtered.get(key) == payload[key], key


# -- the bootstrap manifest ----------------------------------------------------------------------

_RUNTIME_NAMES = {field_class.value for field_class in FieldClass}
_PLUGIN_DEFAULT = {"SYSTEM", "BEHAVIORAL", "CODE_METADATA"}


def _plugin_allowed(manifest_classes: list[str]) -> set[str]:
    """What the IDE plugin allows for a manifest list.

    Mirrors ``ResearchSessionManager.privacyPolicyFor`` in the plugin
    (code4me2/src/main/kotlin/me/code4me/research/session/): names resolve
    through ``FieldClass.fromWire`` (runtime vocabulary only), SECRET is
    removed, and an empty result falls back to the metadata default. The
    plugin writes that policy for the ACP proxy, which filters before upload.
    """
    declared = {name.strip().upper() for name in manifest_classes} & _RUNTIME_NAMES
    if not declared:
        return set(_PLUGIN_DEFAULT)
    return declared - {"SECRET"}


def _manifest_classes(classes: list[str]) -> list[str]:
    study = SimpleNamespace(
        research_config_json={"telemetry_policy": {"allowed_field_classes": classes, "content_capture": True}}
    )
    enrollment = SimpleNamespace(
        status=EnrollmentStatus.ACTIVE, consent_accepted_at=datetime(2026, 9, 20, tzinfo=timezone.utc)
    )
    return _policies(study, enrollment).telemetry_policy.allowed_field_classes


@pytest.mark.parametrize(
    "classes",
    [
        ["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA"],  # website "Custom" default
        ["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA", "CONTENT"],  # "Everything"
        ["STRUCTURAL", "METRICS", "DIAGNOSTICS"],
        ["METRICS"],
        ["SYSTEM", "BEHAVIORAL"],
        ["NOT_A_CLASS"],
        ["SECRET"],
        [],
    ],
)
def test_the_plugin_resolves_the_manifest_to_the_classes_ingestion_allows(classes):
    manifest = _manifest_classes(classes)
    assert set(manifest) <= _RUNTIME_NAMES - {"SECRET"}
    server = PrivacyPolicy.from_study_policy({"allowed_field_classes": classes}, consent_active=True)
    assert _plugin_allowed(manifest) == {field_class.value for field_class in server.allowed_field_classes}


def test_the_website_presets_keep_structural_metadata_on_the_client():
    assert _manifest_classes(["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA"]) == [
        "BEHAVIORAL",
        "CODE_METADATA",
        "SYSTEM",
    ]
    assert _manifest_classes(["STRUCTURAL", "METRICS", "DIAGNOSTICS", "CODE_METADATA", "CONTENT"]) == [
        "BEHAVIORAL",
        "CODE_METADATA",
        "CONTENT",
        "SYSTEM",
    ]


def test_a_study_cannot_declare_secret():
    from fastapi import HTTPException

    from backend.routers.research.studies import _validated_telemetry_policy

    with pytest.raises(HTTPException) as refused:
        _validated_telemetry_policy({"allowed_field_classes": ["STRUCTURAL", "SECRET"]})
    assert refused.value.status_code == 422
    assert refused.value.detail["code"] == "TELEMETRY_POLICY_INVALID"
    # Both vocabularies are still accepted.
    policy = {"allowed_field_classes": ["STRUCTURAL", "METRICS", "SYSTEM", "CODE_METADATA", "CONTENT"]}
    assert _validated_telemetry_policy(dict(policy)) == policy


_RESOLUTION = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/research/telemetry_policy_resolution.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _RESOLUTION, ids=[str(case["declared"]) for case in _RESOLUTION])
def test_the_shared_resolution_table_matches_ingestion(case):
    # The website's resolver (studyUtils.resolveFieldClasses) is tested against
    # the same table, so what the UI says is collected matches ingestion.
    policy = PrivacyPolicy.from_study_policy({"allowed_field_classes": case["declared"]}, consent_active=True)
    assert sorted(field_class.value for field_class in policy.allowed_field_classes) == case["runtime"]
