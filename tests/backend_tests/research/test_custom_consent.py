"""Custom consent forms: validation, the accepted view and its version digest."""

from __future__ import annotations

import pytest

from research.study.consent import (
    DEFAULT_STATEMENT,
    ConsentError,
    accept,
    build_view,
    custom_consent,
    validate_consent_config,
    view_digest,
)

FORM = {
    "document": "About this study\r\n\r\nWe compare two coding agents.\nContact: https://example.org",
    "statements": [
        {"id": "participate", "text": "I agree to take part.", "required": True},
        {"id": "future-use", "text": "My anonymised data may be reused.", "required": False},
    ],
}


def test_validation_normalizes_and_freezes_a_custom_form():
    config = validate_consent_config(FORM)
    assert config["document"].startswith("About this study\n\nWe compare")
    assert "\r" not in config["document"]
    assert [statement["id"] for statement in config["statements"]] == ["participate", "future-use"]
    assert custom_consent({"consent": config}) is not None
    assert custom_consent({}) is None


@pytest.mark.parametrize(
    "form, field",
    [
        ({"document": "", "statements": FORM["statements"]}, "document"),
        ({"document": "x" * 20_001, "statements": FORM["statements"]}, "document"),
        ({"document": "bell\x07", "statements": FORM["statements"]}, "document"),
        ({"document": "ok", "statements": []}, "statements"),
        ({"document": "ok", "statements": [{"id": "A", "text": "t"}]}, "statements.0.id"),
        ({"document": "ok", "statements": [{"id": "a", "text": "x" * 501}]}, "statements.0.text"),
        ({"document": "ok", "statements": [{"id": "a", "text": "t", "required": False}]}, "consent"),
        (
            {"document": "ok", "statements": [{"id": "a", "text": "t"}, {"id": "a", "text": "u"}]},
            "consent",
        ),
        ({"document": "ok", "statements": [{"id": "a", "text": "t"}], "extra": 1}, "extra"),
        (
            {"document": "ok", "statements": [{"id": f"s{index}", "text": "t"} for index in range(21)]},
            "statements",
        ),
    ],
)
def test_invalid_forms_are_refused_with_the_field(form, field):
    with pytest.raises(ConsentError) as raised:
        validate_consent_config(form)
    assert raised.value.code == "CONSENT_INVALID"
    assert raised.value.status_code == 422
    assert raised.value.field == field


def test_stock_view_has_the_original_single_statement():
    view = build_view({}, "platform notice")
    assert view == {
        "version": 1,
        "document": None,
        "notice": "platform notice",
        "statements": [DEFAULT_STATEMENT],
    }


def test_digest_is_stable_and_changes_with_any_part_of_the_view():
    config = {"consent": validate_consent_config(FORM)}
    view = build_view(config, "notice")
    assert view_digest(view) == view_digest(build_view(config, "notice"))
    assert view_digest(view) != view_digest(build_view(config, "another notice"))
    changed = validate_consent_config({**FORM, "document": FORM["document"] + "!"})
    assert view_digest(view) != view_digest(build_view({"consent": changed}, "notice"))


def test_custom_form_acceptance_checks_digest_and_required_statements():
    view = build_view({"consent": validate_consent_config(FORM)}, "notice")
    digest = view_digest(view)

    for given in (None, "0" * 64):
        with pytest.raises(ConsentError) as raised:
            accept(view, given, {"participate": True})
        assert raised.value.code == "CONSENT_CHANGED"

    with pytest.raises(ConsentError) as raised:
        accept(view, digest, {"participate": True, "unknown": True})
    assert (raised.value.code, raised.value.status_code) == ("CONSENT_STATEMENTS_INVALID", 422)

    with pytest.raises(ConsentError) as raised:
        accept(view, digest, {"future-use": True})
    assert raised.value.code == "CONSENT_STATEMENTS_REQUIRED"
    assert raised.value.detail()["missing"] == ["participate"]

    accepted_digest, snapshot = accept(view, digest, {"participate": True})
    assert accepted_digest == digest
    assert snapshot["answers"] == {"participate": True, "future-use": False}
    assert snapshot["document"] == view["document"]
    assert snapshot["notice"] == "notice"


def test_stock_form_still_accepts_the_single_checkbox_of_older_clients():
    view = build_view({}, "notice")
    digest, snapshot = accept(view, None, None)
    assert digest == view_digest(view)
    assert snapshot["answers"] == {"accept": True}
    # A current client sends the digest and the statement it ticked.
    assert accept(view, digest, {"accept": True})[1]["answers"] == {"accept": True}
    with pytest.raises(ConsentError):
        accept(view, digest, {"accept": False})
