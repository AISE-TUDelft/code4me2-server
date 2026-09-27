"""The join consent notice states what the study's frozen policy stores.

It is composed from the policy as ingestion resolves it
(``PrivacyPolicy.from_study_policy``): tool titles and error messages are named
whenever behavioural metadata is kept, code metadata is described as stored in
clear or only as hashes, and content only with ``content_capture``.
"""

from __future__ import annotations

from backend.routers.research.join import (
    CODE_METADATA_CONSENT_TEXT,
    CONTENT_CAPTURE_CONSENT_TEXT,
    GLOBAL_CONSENT_TEXT,
    HASHED_CODE_METADATA_CONSENT_TEXT,
    METADATA_ONLY_CONSENT_TEXT,
    METADATA_ONLY_TITLES_CAVEAT,
    TOOL_TITLES_CONSENT_TEXT,
    consent_text,
)

METADATA_ONLY_WITH_TITLES = METADATA_ONLY_CONSENT_TEXT + METADATA_ONLY_TITLES_CAVEAT + "."


def test_the_default_policy_discloses_titles_and_code_metadata():
    text = consent_text({})
    assert text.startswith(GLOBAL_CONSENT_TEXT)
    assert TOOL_TITLES_CONSENT_TEXT in text
    assert CODE_METADATA_CONSENT_TEXT in text
    assert text.endswith(METADATA_ONLY_WITH_TITLES)
    assert "command lines" in text and "file paths" in text
    assert "randomly assigned" in text and "AI model provider" in text


def test_code_metadata_that_is_not_allowed_is_described_as_hashed():
    text = consent_text({"allowed_field_classes": ["STRUCTURAL", "METRICS", "DIAGNOSTICS"]})
    assert TOOL_TITLES_CONSENT_TEXT in text
    assert HASHED_CODE_METADATA_CONSENT_TEXT in text
    assert CODE_METADATA_CONSENT_TEXT not in text


def test_titles_are_only_named_when_behavioural_metadata_is_kept():
    text = consent_text({"allowed_field_classes": ["METRICS"]})
    assert TOOL_TITLES_CONSENT_TEXT not in text
    # No caveat about titles that are not stored.
    assert METADATA_ONLY_TITLES_CAVEAT not in text
    assert text.endswith(METADATA_ONLY_CONSENT_TEXT + ".")


def test_content_capture_follows_the_flag_not_the_declared_classes():
    assert consent_text({"allowed_field_classes": ["STRUCTURAL"], "content_capture": True}).endswith(
        CONTENT_CAPTURE_CONSENT_TEXT
    )
    assert consent_text({"allowed_field_classes": ["CONTENT"]}).endswith(METADATA_ONLY_CONSENT_TEXT + ".")
    # A malformed stored policy falls back to the default notice.
    assert consent_text(None) == consent_text({})
