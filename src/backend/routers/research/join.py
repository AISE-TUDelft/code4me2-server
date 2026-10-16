"""Web-only research study join and consent."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from App import App
from backend.Responses import JsonResponseWithStatus
from backend.routers.analytics.auth_utils import AuthenticatedUser, get_current_user
from privacy import collection
from research.study.lifecycle import (
    AlreadyEnrolledError,
    ActiveEnrollmentError,
    StudyStoppedError,
    open_study_enrollment,
)
from research.runtime.assignment.hashing import manual_override_enabled
from research.study import consent
from research.study.protocol import store as study_store
from research.telemetry.enums import FieldClass
from research.telemetry.privacy.engine import PrivacyPolicy

router = APIRouter()

# The consent notice is composed from the study's frozen policy as ingestion
# resolves it (PrivacyPolicy.from_study_policy), so it states what is stored.
GLOBAL_CONSENT_TEXT = (
    "This study collects metadata about how you use the research agent and the "
    "IDE: which events happen and when (for example prompts you send, tools the "
    "agent runs, approvals, errors, and files you open, edit and save), with "
    "timings, counts and token usage. You are randomly assigned to one of the "
    "study's agent configurations. The research agent sends your prompts and the "
    "code it works with to an AI model provider to produce its responses, "
    "whether or not this study stores them. Provider credentials are never "
    "collected, and values that look like secrets (such as API keys and tokens) "
    "are removed, although that filter only recognises common formats. Your "
    "study-local pseudonym is used in research data; your account identity "
    "remains private."
)

#: The assignment sentence of ``GLOBAL_CONSENT_TEXT`` and its replacement for a
#: study whose protocol lets the owner set an arm by hand before first use.
RANDOM_ASSIGNMENT_SENTENCE = "You are randomly assigned to one of the study's agent configurations."
MANUAL_ASSIGNMENT_SENTENCE = (
    "You are assigned to one of the study's agent configurations, at random or by "
    "the research team."
)

TOOL_TITLES_CONSENT_TEXT = (
    "The metadata includes the titles of the agent's tool calls and its error "
    "messages, which can contain command lines, file paths (which can include "
    "your computer's user name), search terms and URLs."
)

CODE_METADATA_CONSENT_TEXT = (
    "The metadata also includes code metadata such as file paths, file types, "
    "languages and symbol names."
)

HASHED_CODE_METADATA_CONSENT_TEXT = (
    "Code metadata such as file types and languages is stored only as hashes, "
    "which can be reversed for common values."
)

METADATA_ONLY_CONSENT_TEXT = (
    "This study is configured for metadata only: the text of your prompts, the "
    "agent's responses and reasoning, tool arguments and results, and file "
    "contents are not stored"
)

#: Appended to the metadata-only sentence when tool titles are kept.
METADATA_ONLY_TITLES_CAVEAT = ", apart from what tool titles and error messages may contain"

CONTENT_CAPTURE_CONSENT_TEXT = (
    "This study is configured to store content: your prompts, the agent's "
    "responses and reasoning, tool arguments and results, and file contents may "
    "be stored under the study's approved telemetry policy."
)


def consent_text(telemetry_policy: Any, *, manual_assignment: bool = False) -> str:
    """The participant-facing notice for a frozen study telemetry policy."""
    raw = telemetry_policy if isinstance(telemetry_policy, dict) else {}
    allowed = set(
        PrivacyPolicy.from_study_policy(raw, consent_active=True).allowed_field_classes
    )
    titles_kept = FieldClass.BEHAVIORAL in allowed
    parts = [
        GLOBAL_CONSENT_TEXT.replace(RANDOM_ASSIGNMENT_SENTENCE, MANUAL_ASSIGNMENT_SENTENCE)
        if manual_assignment
        else GLOBAL_CONSENT_TEXT
    ]
    if titles_kept:
        parts.append(TOOL_TITLES_CONSENT_TEXT)
    parts.append(
        CODE_METADATA_CONSENT_TEXT
        if FieldClass.CODE_METADATA in allowed
        else HASHED_CODE_METADATA_CONSENT_TEXT
    )
    if raw.get("content_capture") is True:
        parts.append(CONTENT_CAPTURE_CONSENT_TEXT)
    else:
        parts.append(
            METADATA_ONLY_CONSENT_TEXT
            + (METADATA_ONLY_TITLES_CAVEAT if titles_kept else "")
            + "."
        )
    return " ".join(parts)


def _collection_policy(study: Any) -> dict[str, Any]:
    """Project the frozen study telemetry policy without exposing internals."""
    config = getattr(study, "research_config_json", None) or {}
    raw = config.get("telemetry_policy") or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "content_capture": raw.get("content_capture") is True,
        "allowed_field_classes": list(raw.get("allowed_field_classes") or []),
    }


def study_consent_view(config: Any) -> dict[str, Any]:
    """The consent view of a study: its own form (if any) plus the platform notice."""
    config = config if isinstance(config, dict) else {}
    notice = consent_text(
        config.get("telemetry_policy"),
        manual_assignment=manual_override_enabled(config),
    )
    return consent.build_view(config, notice)


def _consent_payload(study: Any) -> dict[str, Any]:
    """Render the actual frozen collection policy in participant-facing text."""
    config = getattr(study, "research_config_json", None) or {}
    view = study_consent_view(config)
    return {
        # ``text`` stays the platform notice so older clients keep working.
        "text": view["notice"],
        "collection_policy": _collection_policy(study),
        "custom": view["document"] is not None,
        "document": view["document"],
        "notice": view["notice"],
        "statements": view["statements"],
        "digest": consent.view_digest(view),
    }


def _require_authenticated_user(current_user: AuthenticatedUser) -> AuthenticatedUser:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return current_user


class JoinRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    join_code: str = Field(min_length=1)
    accept_consent: bool = False
    # The digest of the consent view the participant reviewed, and which of its
    # statements they ticked. Required for a study with its own consent form.
    consent_digest: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    consent_statements: Optional[dict[str, bool]] = Field(default=None, max_length=20)


def _study_from_code(db: Any, join_code: str):
    return study_store.get_study_by_join_code(db, join_code)


def _study_payload(study: Any) -> dict[str, Any]:
    return {
        "study_id": str(study.study_id),
        "name": study.name,
        "description": study.description,
        "research_status": getattr(study, "research_status", None),
    }


@router.get("/{join_code}", summary="Resolve a study-owned join code")
def resolve_join_code(
    join_code: str,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    _require_authenticated_user(current_user)
    db = app.get_db_session()
    try:
        study = _study_from_code(db, join_code)
        if study is None:
            raise HTTPException(status_code=404, detail="Join code not found")
        if getattr(study, "research_status", None) == "STUDY_STOPPED":
            raise HTTPException(
                status_code=409,
                detail={"code": "STUDY_STOPPED", "message": "the study has been stopped"},
            )
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "join_code": study.join_code,
                "study": _study_payload(study),
                "consent": _consent_payload(study),
            },
        )
    finally:
        db.close()


@router.post("", summary="Accept consent and enroll in a study")
def redeem_join_code(
    payload: JoinRequestBody,
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance),
):
    current_user = _require_authenticated_user(current_user)
    if not payload.accept_consent:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONSENT_REQUIRED", "message": "accept consent before joining"},
        )
    db = app.get_db_session()
    try:
        # Joining and opting out serialize on the account row: this share lock is
        # held until the enrollment commits, and an opt-out updates that row.
        if not collection.lock_collection_allowed(db, current_user.user_id):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DATA_COLLECTION_OPTED_OUT",
                    "message": "you have opted out of data collection; turn it back on to join a study",
                },
            )
        study = _study_from_code(db, payload.join_code)
        if study is None:
            raise HTTPException(status_code=404, detail="join code not found")
        try:
            consent_digest, consent_snapshot = consent.accept(
                study_consent_view(getattr(study, "research_config_json", None)),
                payload.consent_digest,
                payload.consent_statements,
            )
        except consent.ConsentError as error:
            raise HTTPException(status_code=error.status_code, detail=error.detail()) from error
        try:
            result = open_study_enrollment(
                db,
                current_user.user_id,
                payload.join_code,
                consent_digest=consent_digest,
                consent_snapshot=consent_snapshot,
            )
        except StudyStoppedError as error:
            code = "STUDY_STOPPED"
            raise HTTPException(status_code=409, detail={"code": code, "message": str(error)}) from error
        except ActiveEnrollmentError as error:
            code = "ACTIVE_ENROLLMENT_EXISTS"
            raise HTTPException(status_code=409, detail={"code": code, "message": str(error)}) from error
        except AlreadyEnrolledError as error:
            code = "ALREADY_ENROLLED"
            raise HTTPException(status_code=409, detail={"code": code, "message": str(error)}) from error
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return JsonResponseWithStatus(
            status_code=201 if result.created else 200,
            content={
                "enrollment_id": str(result.enrollment_id),
                "study_id": str(result.study_id),
                "assignment_id": str(result.assignment_id) if result.assignment_id else None,
                "agent_profile_id": str(result.agent_profile_id) if result.agent_profile_id else None,
                "status": "ACTIVE",
                "created": result.created,
                "reused": result.reused,
            },
        )
    finally:
        db.close()
