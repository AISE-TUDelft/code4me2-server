"""Researcher-authored consent forms and the version each participant accepts.

A study may freeze a custom consent ``document`` and tick-box ``statements`` in
its configuration (``research_config_json["consent"]``); without one the stock
notice applies with a single required statement. Either way the participant
also sees the platform notice, which states what the platform records (composed
from the frozen telemetry policy by ``backend.routers.research.join``).

The *view* a participant is shown (document, notice and statements) is digested,
and the enrollment stores that digest plus the view with the participant's
answers, so every enrollment records exactly what it agreed to. The document is
plain text: it is rendered with its line breaks and links, never as HTML.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from research.canonical import canonical_hash

__all__ = [
    "CONSENT_VIEW_VERSION",
    "DEFAULT_STATEMENT",
    "ConsentConfigV1",
    "ConsentError",
    "ConsentStatementV1",
    "accept",
    "build_view",
    "custom_consent",
    "validate_consent_config",
    "view_digest",
]

CONSENT_VIEW_VERSION = 1
MAX_DOCUMENT_CHARS = 20_000
MAX_STATEMENTS = 20
MAX_STATEMENT_CHARS = 500

#: The stock form's single statement (the label of the original checkbox).
DEFAULT_STATEMENT: dict[str, Any] = {
    "id": "accept",
    "text": "I accept the study consent notice.",
    "required": True,
}

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _normalized_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if _CONTROL_CHARACTERS.search(text):
        raise ValueError("must not contain control characters")
    return text


class ConsentStatementV1(BaseModel):
    """One tick-box statement; ``required`` ones must be ticked to join."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    text: str
    required: bool = True

    @field_validator("text")
    @classmethod
    def _text(cls, value: str) -> str:
        text = _normalized_text(value)
        if not 1 <= len(text) <= MAX_STATEMENT_CHARS:
            raise ValueError(f"must be 1-{MAX_STATEMENT_CHARS} characters")
        return text


class ConsentConfigV1(BaseModel):
    """A study's custom consent form, frozen with its configuration."""

    model_config = ConfigDict(extra="forbid")

    document: str
    statements: list[ConsentStatementV1] = Field(min_length=1, max_length=MAX_STATEMENTS)

    @field_validator("document")
    @classmethod
    def _document(cls, value: str) -> str:
        text = _normalized_text(value)
        if not 1 <= len(text) <= MAX_DOCUMENT_CHARS:
            raise ValueError(f"must be 1-{MAX_DOCUMENT_CHARS} characters")
        return text

    @model_validator(mode="after")
    def _statements(self) -> "ConsentConfigV1":
        ids = [statement.id for statement in self.statements]
        if len(set(ids)) != len(ids):
            raise ValueError("statement ids must be unique")
        if not any(statement.required for statement in self.statements):
            raise ValueError("at least one statement must be required")
        return self


class ConsentError(Exception):
    """A refused consent configuration or acceptance, mapped to an API error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        field: Optional[str] = None,
        missing: Optional[list[str]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.field = field
        self.missing = missing or []

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.field:
            detail["field"] = self.field
        if self.missing:
            detail["missing"] = self.missing
        return detail


def validate_consent_config(raw: Any) -> dict[str, Any]:
    """Validate a create request's custom form; return its normalized JSON."""
    try:
        return ConsentConfigV1.model_validate(raw).model_dump(mode="json")
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "consent"
        raise ConsentError(
            "CONSENT_INVALID",
            f"{field}: {first.get('msg', 'invalid consent form')}",
            status_code=422,
            field=field,
        ) from error


def custom_consent(config: Any) -> Optional[ConsentConfigV1]:
    """The study's frozen custom form, or ``None`` for the stock notice."""
    raw = config.get("consent") if isinstance(config, Mapping) else None
    if not raw:
        return None
    return ConsentConfigV1.model_validate(raw)


def build_view(config: Any, notice: str) -> dict[str, Any]:
    """What a participant is shown and accepts for one study."""
    custom = custom_consent(config)
    return {
        "version": CONSENT_VIEW_VERSION,
        "document": custom.document if custom is not None else None,
        "notice": notice,
        "statements": (
            [statement.model_dump(mode="json") for statement in custom.statements]
            if custom is not None
            else [dict(DEFAULT_STATEMENT)]
        ),
    }


def view_digest(view: Mapping[str, Any]) -> str:
    """The version identity of a consent view."""
    return canonical_hash(dict(view))


def accept(
    view: Mapping[str, Any],
    digest: Optional[str],
    answers: Optional[Mapping[str, bool]],
) -> tuple[str, dict[str, Any]]:
    """Check one acceptance; return ``(digest, snapshot)`` to store on the enrollment.

    A custom form must be accepted against the digest the participant saw. The
    stock form may still be accepted without one (older clients tick a single
    box), in which case its one statement counts as ticked.
    """
    expected = view_digest(view)
    statements = list(view.get("statements") or [])
    custom = view.get("document") is not None
    if digest is not None and digest != expected:
        raise ConsentError("CONSENT_CHANGED", "the consent form changed; review it again")
    if digest is None and custom:
        raise ConsentError("CONSENT_CHANGED", "review the study's consent form before joining")
    given = dict(answers or {})
    if not custom and digest is None and not given:
        given = {statement["id"]: True for statement in statements}
    unknown = sorted(set(given) - {statement["id"] for statement in statements})
    if unknown:
        raise ConsentError(
            "CONSENT_STATEMENTS_INVALID",
            f"unknown consent statements: {', '.join(unknown)}",
            status_code=422,
        )
    missing = [
        statement["id"]
        for statement in statements
        if statement.get("required") and given.get(statement["id"]) is not True
    ]
    if missing:
        raise ConsentError(
            "CONSENT_STATEMENTS_REQUIRED",
            "tick every required consent statement to join",
            missing=missing,
        )
    snapshot = {
        **dict(view),
        "answers": {statement["id"]: given.get(statement["id"]) is True for statement in statements},
    }
    return expected, snapshot
