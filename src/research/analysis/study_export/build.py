"""Build a study export ZIP: one file per dataset plus ``manifest.json``.

Every dataset is described by its column specs, which also become the
manifest's data dictionary. CSV cells are guarded against spreadsheet formula
injection; JSONL events carry the stored canonical envelope, from which account
linking keys are always removed and content is removed unless asked for.
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

from research.analysis.study_analytics.metrics import (
    METRIC_KEYS,
    display_tool_name,
    permission_decision,
)
from research.analysis.study_analytics.models import EventRow
from research.telemetry.enums import FieldClass
from research.telemetry.privacy.engine import PrivacyPolicy, filter_payload

__all__ = [
    "CHAT_COLUMNS",
    "DATASETS",
    "EVENT_CSV_COLUMNS",
    "EVENT_JSONL_COLUMNS",
    "FORMATS",
    "PARTICIPANT_COLUMNS",
    "SESSION_COLUMNS",
    "Column",
    "ExportWriter",
    "csv_cell",
    "event_csv_row",
    "export_envelope",
    "metric_columns",
]

DATASETS = ("participants", "participant_metrics", "sessions", "chats", "events")
FORMATS = ("csv", "jsonl")
SPOOL_MAX_BYTES = 32 * 1024 * 1024

#: Keys that could link a record to an account; never exported.
LINKABLE_KEYS = ("account_id", "email", "session_token")
#: Spreadsheet formula triggers: a text cell starting with one is prefixed with '.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

#: Re-filtering stored payloads for an export without content: metadata is
#: kept as stored, every content-class value becomes ``[REDACTED]``.
_WITHOUT_CONTENT = PrivacyPolicy(
    allowed_field_classes=[FieldClass.SYSTEM, FieldClass.BEHAVIORAL, FieldClass.CODE_METADATA],
    content_allowed=False,
    consent_active=True,
    code_metadata_mode="allow",
)


@dataclass(frozen=True)
class Column:
    name: str
    description: str


PARTICIPANT_COLUMNS = (
    Column("enrollment_id", "Study-local enrollment id (the randomisation unit)."),
    Column("participant_code", "Study-local pseudonym."),
    Column("status", "Enrollment status (ACTIVE, REVOKED, WITHDRAWN, COMPLETED…)."),
    Column("enrolled_at", "When the participant joined (UTC)."),
    Column("consent_accepted_at", "When consent was accepted (UTC)."),
    Column("consent_digest", "Version digest of the consent view accepted (null before versions were recorded)."),
    Column("consent_answers", "Ticked consent statements as JSON {statement id: true/false}."),
    Column("arm_profile_id", "Assigned arm (agent profile id)."),
    Column("arm_name", "Assigned arm name."),
    Column("arm_model", "Model of the assigned arm."),
    Column("arm_runtime", "Runtime of the assigned arm."),
    Column("assignment_strategy", "DETERMINISTIC_HASH or RANDOM_EQUAL (the draw), MANUAL (set by the owner before first use)."),
    Column("assigned_at", "When the arm was assigned (UTC)."),
    Column(
        "randomized_profile_id",
        "Arm the salted-hash draw gives this enrollment (studies with DETERMINISTIC_HASH; recomputable, "
        "see manifest.assignment); differs from arm_profile_id only after a manual override.",
    ),
)

SESSION_COLUMNS = (
    Column("session_id", "Research session id (one IDE project activity boundary)."),
    Column("enrollment_id", "Enrollment of the session."),
    Column("participant_code", "Study-local pseudonym."),
    Column("state", "Final or current session state."),
    Column("opened_at", "When the session opened (UTC)."),
    Column("closed_at", "When it closed (UTC), if it did."),
    Column("last_activity_at", "Last explicit activity (UTC); a close the server applies stamps its own time here."),
    Column("last_heartbeat_at", "Last heartbeat from the IDE (UTC): the session was still running then."),
    Column("close_reason", "Why it closed."),
    Column(
        "session_seconds",
        "This session's time as the analytics count it, within the exported range: opened_at to closed_at, ending "
        "at last_heartbeat_at at the latest when the server closed the session on its own (idle_timeout, "
        "resume_grace_expired, revoked, STUDY_STOPPED), which it does only when it next hears from the participant. "
        "A participant's session time (participant_metrics) merges sessions that overlap (two windows open at "
        "once), so it can be less than the sum of their rows.",
    ),
)

CHAT_COLUMNS = (
    Column("enrollment_id", "Enrollment of the chat."),
    Column("participant_code", "Study-local pseudonym."),
    Column("arm_profile_id", "Assigned arm."),
    Column("chat_id", "ACP chat (session) id."),
    Column(
        "ordinal",
        "Chat number in the participant's history (within the exported range); empty for a chat without a prompt.",
    ),
    Column(
        "used",
        "Whether the participant sent a prompt in the chat. IntelliJ also starts an agent for a chat it only creates "
        "or shows (a new empty chat, the chat it switches to after a delete); those are not numbered or counted.",
    ),
    Column("started_at", "First start or event of the chat (UTC)."),
    Column("ended_at", "When the chat ended (UTC), if observed."),
    Column("start_kind", "new, fork, load (reopened), resume, or unknown (older telemetry)."),
    Column(
        "end_reason",
        "close (the IDE closed the chat), host_closed / signal_terminated (the IDE ended the agent process: the "
        "chat was deleted or the IDE closed), agent_exited, session_stale, open, or unknown.",
    ),
    Column("reopen_count", "Times the chat was reopened or resumed."),
    Column("prompts", "Prompts in the chat."),
    Column("tool_calls", "Tool calls in the chat."),
    Column("cancels", "Turns interrupted by the participant."),
    Column("rejections", "Approval requests rejected."),
    Column("revisions", "Approval requests where the participant chose Revise… (even if the follow-up form was dismissed)."),
    Column("usage_tokens", "Provider-reported tokens (when reported)."),
    Column("previous_end_reason", "How the participant's previous chat ended."),
    Column("gap_since_previous_seconds", "Seconds between the previous chat's end and this chat's start."),
)

EVENT_CSV_COLUMNS = (
    Column("event_id", "Canonical event id."),
    Column("occurred_at", "When the event happened (UTC)."),
    Column("enrollment_id", "Enrollment of the event."),
    Column("participant_code", "Study-local pseudonym."),
    Column("arm_profile_id", "Assigned arm."),
    Column("research_session_id", "Research session."),
    Column("chat_id", "ACP chat (session) id, when the event belongs to one."),
    Column("event_type", "Canonical event type."),
    Column("source", "acp (proxy), relay (built-in agent), ide or server."),
    Column("emitter_id", "Emitting process."),
    Column("emitter_sequence", "Per-emitter sequence number (orders events of one emitter)."),
    Column("lifecycle_state", "Lifecycle phase (started, completed, cancelled…)."),
    Column("turn_id", "Prompt turn within the emitter."),
    Column("tool_call_id", "Tool call id."),
    Column("permission_id", "Approval request id."),
    Column("message_id", "Streamed message id."),
    Column("message_kind", "user (prompt text), assistant or thought (streamed chunks); empty on turn starts."),
    Column("tool_name", "Tool identifier (titles that describe a command or file are left out)."),
    Column("tool_kind", "ACP tool kind (read, edit, execute…)."),
    Column("status", "Tool status."),
    Column("decision", "Approval decision: allow, reject, revise (Revise… chosen), cancelled…"),
    Column("selected_option_id", "Approval option picked."),
    Column("stop_reason", "Why a turn ended."),
    Column("error_code", "Error code."),
    Column("acp_method", "ACP method the event was derived from."),
    Column("end_reason", "Why a chat's agent process ended."),
    Column("usage_tokens", "Provider-reported tokens."),
    Column("latency_ms", "Reported latency (ms)."),
)

EVENT_JSONL_COLUMNS = (
    Column("event_id", "Canonical event id."),
    Column("occurred_at", "When the event happened (UTC)."),
    Column("enrollment_id", "Enrollment of the event."),
    Column("participant_code", "Study-local pseudonym."),
    Column("arm_profile_id", "Assigned arm."),
    Column("research_session_id", "Research session."),
    Column("event_type", "Canonical event type."),
    Column("source", "Event source."),
    Column(
        "envelope",
        "The stored canonical envelope (payload, correlations, metrics, provenance). Content-class values are "
        "[REDACTED] unless the export includes content; account-linking keys are always removed.",
    ),
)

_METRIC_HELP = "Participant-level metric (see the study analytics definitions)."


def metric_columns() -> tuple[Column, ...]:
    return (
        Column("enrollment_id", "Enrollment."),
        Column("participant_code", "Study-local pseudonym."),
        Column("arm_profile_id", "Assigned arm."),
        Column("has_telemetry", "Whether any event was retained for the participant in range."),
        *(Column(key, _METRIC_HELP) for key in METRIC_KEYS),
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def csv_cell(value: Any) -> str:
    """One CSV cell; text that a spreadsheet would run as a formula is quoted."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, default=_json_default)
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _without_linkable(mapping: Any) -> dict[str, Any]:
    return {key: value for key, value in dict(mapping or {}).items() if key not in LINKABLE_KEYS}


def export_envelope(envelope: Mapping[str, Any], *, include_content: bool) -> dict[str, Any]:
    """The stored envelope for an export: never account-linking keys, and
    content only when asked for."""
    exported = dict(envelope)
    payload = _without_linkable(exported.get("payload"))
    if not include_content:
        payload, summary = filter_payload(payload, _WITHOUT_CONTENT)
        if summary.blocked:
            payload = {}
    exported["payload"] = payload
    exported["provenance"] = _without_linkable(exported.get("provenance"))
    return exported


def event_csv_row(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """The metadata projection of one envelope (no content-class values)."""
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), Mapping) else {}
    correlations = envelope.get("correlations") if isinstance(envelope.get("correlations"), Mapping) else {}
    metrics = envelope.get("metrics") if isinstance(envelope.get("metrics"), Mapping) else {}
    decision_row = EventRow(
        event_type=str(envelope.get("event_type") or ""),
        source=str(envelope.get("source") or ""),
        occurred_at=datetime.min,
        decision=payload.get("decision"),
        selected_option_id=payload.get("selected_option_id"),
    )
    return {
        "chat_id": payload.get("session_id"),
        "lifecycle_state": envelope.get("lifecycle_state"),
        "turn_id": correlations.get("turn_id"),
        "tool_call_id": correlations.get("tool_call_id") or payload.get("tool_call_id"),
        "permission_id": correlations.get("permission_id"),
        "message_id": correlations.get("message_id") or payload.get("message_id"),
        "message_kind": payload.get("message_kind"),
        "tool_name": display_tool_name(payload.get("tool_name")),
        "tool_kind": payload.get("tool_kind"),
        "status": payload.get("status"),
        "decision": permission_decision(decision_row) if payload.get("decision") is not None else None,
        "selected_option_id": payload.get("selected_option_id"),
        "stop_reason": payload.get("stop_reason"),
        "error_code": payload.get("error_code"),
        "acp_method": payload.get("acp_method"),
        "end_reason": payload.get("end_reason"),
        "usage_tokens": metrics.get("usage_tokens"),
        "latency_ms": metrics.get("latency_ms"),
    }


class ExportWriter:
    """Writes datasets into a ZIP spooled to a temporary file (bounded memory)."""

    def __init__(self, export_format: str) -> None:
        self.format = export_format
        self.file = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
        self.zip = zipfile.ZipFile(self.file, "w", compression=zipfile.ZIP_DEFLATED)
        self.counts: dict[str, int] = {}

    def write_rows(self, name: str, columns: Sequence[Column], rows: Iterable[Mapping[str, Any]]) -> int:
        filename = f"{name}.{self.format}"
        count = 0
        with self.zip.open(filename, "w", force_zip64=True) as raw:
            stream = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            if self.format == "csv":
                writer = csv.writer(stream)
                writer.writerow([column.name for column in columns])
                for row in rows:
                    writer.writerow([csv_cell(row.get(column.name)) for column in columns])
                    count += 1
            else:
                for row in rows:
                    stream.write(
                        json.dumps(
                            {column.name: row.get(column.name) for column in columns},
                            ensure_ascii=False,
                            default=_json_default,
                        )
                    )
                    stream.write("\n")
                    count += 1
            stream.flush()
            stream.detach()
        self.counts[filename] = count
        return count

    def write_json(self, name: str, document: Any) -> None:
        self.zip.writestr(name, json.dumps(document, indent=2, ensure_ascii=False, default=_json_default))

    def finish(self) -> Any:
        self.zip.close()
        self.file.seek(0)
        return self.file


def dictionary(columns_by_dataset: Mapping[str, Sequence[Column]]) -> dict[str, dict[str, str]]:
    """The manifest's data dictionary: dataset -> column -> description."""
    return {
        dataset: {column.name: column.description for column in columns}
        for dataset, columns in columns_by_dataset.items()
    }


def optional_iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None
