"""Assemble one chat's events into turns of readable blocks (pure).

A turn starts at a participant prompt and runs to the next one; whatever
precedes the first prompt (the chat opening) is a ``preamble``. Inside a turn:
streamed reasoning and message chunks merge into one block per message, the
lifecycle events of a tool call merge into one block, and its approval request
and answer attach to that tool call. Text is returned as stored; a value the
privacy policy removed is reported as not captured, never guessed.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlparse

from research.telemetry.chat_lifecycle import (
    ACP_METHOD_KEY,
    CANCEL_METHOD,
    END_REASON_KEY,
    REVISE_OPTION_ID,
)

if TYPE_CHECKING:
    from .store import TraceRow

__all__ = ["MAX_TEXT_CHARS", "build_page", "content_text", "diff_blocks"]

REDACTED = "[REDACTED]"
#: Longest text returned for one block (the rest is cut and flagged).
MAX_TEXT_CHARS = 20_000
#: A page stops early after this many events even inside one turn.
MAX_PAGE_EVENTS = 20_000

_PROMPT = "agent.message.started"
_COMPLETION = "agent.message.completed"
_TOOL_EVENTS = {"tool.created", "tool.started", "tool.completed", "tool.failed"}
_ERROR_EVENTS = {"agent.error", "system.agent.crashed", "system.proxy.error"}


def _cap(text: Optional[str]) -> tuple[Optional[str], bool]:
    if text is None or len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


def content_text(value: Any) -> tuple[Optional[str], bool]:
    """Readable text of a stored ACP value, and whether policy removed it."""
    if value is None:
        return None, False
    if isinstance(value, str):
        return (None, True) if value == REDACTED else (value, False)
    if isinstance(value, list):
        parts: list[str] = []
        redacted = False
        for item in value:
            text, hidden = content_text(item)
            redacted = redacted or hidden
            if text:
                parts.append(text)
        return ("\n".join(parts) if parts else None), redacted
    if isinstance(value, Mapping):
        kind = value.get("type")
        if kind == "text":
            return content_text(value.get("text"))
        if kind == "content":
            return content_text(value.get("content"))
        if kind == "resource_link":
            return f"[file: {value.get('uri') or value.get('name') or 'resource'}]", False
        if kind == "resource":
            resource = value.get("resource") if isinstance(value.get("resource"), Mapping) else {}
            label = f"[file: {resource.get('uri')}]" if resource.get("uri") else "[file]"
            text, hidden = content_text(resource.get("text"))
            return (f"{label}\n{text}" if text else label), hidden
        if kind in ("image", "audio"):
            return f"[{kind}]", False
        if kind == "terminal":
            return "[terminal output]", False
        if kind == "diff":
            return None, False
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str), False
    return str(value), False


def diff_blocks(value: Any) -> list[dict[str, Any]]:
    """The file diffs a tool call carried, as unified diffs (capped)."""
    items = value if isinstance(value, list) else [value]
    diffs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "diff":
            continue
        old, new = item.get("oldText"), item.get("newText")
        if old == REDACTED or new == REDACTED:
            diffs.append({"path": item.get("path"), "diff": None, "redacted": True})
            continue
        lines = difflib.unified_diff(
            str(old or "").splitlines(),
            str(new or "").splitlines(),
            fromfile="before",
            tofile="after",
            lineterm="",
        )
        text, truncated = _cap("\n".join(lines))
        diffs.append({"path": item.get("path"), "diff": text, "truncated": truncated})
    return diffs


def _decision(payload: Mapping[str, Any]) -> Optional[str]:
    if payload.get("selected_option_id") == REVISE_OPTION_ID:
        return "revise"
    decision = payload.get("decision")
    return str(decision) if decision is not None else None


def _is_turn_start(row: TraceRow) -> bool:
    return (
        row.event_type == _PROMPT
        and row.payload.get("message_kind") is None
        and (row.lifecycle_state or "") != "started"
    )


def _ms(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() * 1000)


@dataclass
class _Turn:
    index: int
    kind: str
    started_at: datetime
    turn_id: Optional[str] = None
    research_session_id: Optional[str] = None
    completed_at: Optional[datetime] = None
    stop_reason: Optional[str] = None
    usage_tokens: Optional[int] = None
    continues: bool = False
    blocks: list[dict[str, Any]] = field(default_factory=list)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    permissions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def block(self, kind: str, at: datetime, **fields: Any) -> dict[str, Any]:
        entry = {"type": kind, "at": at, **fields}
        self.blocks.append(entry)
        return entry

    def serialize(self) -> dict[str, Any]:
        blocks = []
        for block in self.blocks:
            entry = {key: value for key, value in block.items() if not key.startswith("_")}
            at = entry.pop("at")
            entry["at"] = at.isoformat()
            entry["offset_ms"] = _ms(self.started_at, at) or 0
            for moment in ("started_at", "completed_at", "requested_at", "decided_at"):
                if isinstance(entry.get(moment), datetime):
                    entry[moment] = entry[moment].isoformat()
            blocks.append(entry)
        return {
            "index": self.index,
            "kind": self.kind,
            "turn_id": self.turn_id,
            "research_session_id": self.research_session_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": _ms(self.started_at, self.completed_at),
            "stop_reason": self.stop_reason,
            "usage_tokens": self.usage_tokens,
            "continues": self.continues,
            "blocks": blocks,
        }


def _text_fields(value: Any) -> dict[str, Any]:
    text, redacted = content_text(value)
    text, truncated = _cap(text)
    return {"text": text, "redacted": redacted, "truncated": truncated}


def _typed_prompt(value: Any) -> Any:
    """What the participant typed: the prompt's text blocks, without what the IDE
    sent along (IntelliJ links the open file to every prompt)."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping) and item.get("type") == "text"]
    return value


def _prompt_attachments(value: Any) -> list[dict[str, Any]]:
    """Files and media sent with a prompt, by file name only: never the path,
    which names the participant's home folder."""
    attachments: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        kind = item.get("type") if isinstance(item, Mapping) else None
        if kind == "resource_link":
            ref = item.get("name") or item.get("uri")
        elif kind == "resource":
            resource = item.get("resource")
            ref = resource.get("uri") if isinstance(resource, Mapping) else None
        elif kind in ("image", "audio"):
            ref = item.get("uri")
        else:
            continue
        ref = str(ref or "")
        name = PurePosixPath(urlparse(ref).path or ref).name
        attachments.append({"name": name or str(kind), "type": kind})
    return attachments


def _revision_instructions(payload: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """The participant's instructions from a relayed decision's content blob
    (``payload``: the agent's own payload as JSON, or ``[REDACTED]``)."""
    blob = payload.get("payload")
    if blob is None:
        return None
    if blob == REDACTED:
        return _text_fields(REDACTED)
    try:
        own = json.loads(blob) if isinstance(blob, str) else blob
    except ValueError:
        return None
    own = own.get("payload") if isinstance(own, Mapping) else None
    text = own.get("text") if isinstance(own, Mapping) else None
    return _text_fields(text) if text is not None else None


def _apply_revision(turn: _Turn, row: TraceRow) -> None:
    """Attach the built-in agent's report of a "Revise…" form to its tool call:
    how the form was answered, which parts were kept and the participant's
    instructions (content, so only where the study captured it)."""
    payload = row.payload
    call_id = row.correlations.get("tool_call_id") or payload.get("tool_call_id")
    tool = turn.tools.get(call_id) if call_id else None
    if tool is None:
        return
    permission = tool.get("permission")
    if permission is None:
        permission = tool["permission"] = {"permission_id": None, "requested_at": None, "options": []}
    if not permission.get("decision"):
        permission["decision"] = "revise"
    counts = row.metrics.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    instructions = _revision_instructions(payload)
    if payload.get("payload") is None and payload.get("elicitation_action") == "accept":
        # A sent form carries instructions (a required field): a report stored
        # without any content blob is one whose study did not keep them.
        instructions = _text_fields(REDACTED)
    permission["revision"] = {
        "form": payload.get("elicitation_action"),
        "status": payload.get("revise_status"),
        "hunks": counts.get("hunk_count"),
        "kept_hunks": counts.get("kept_hunk_count"),
        "instructions": instructions,
    }


def _apply(turn: _Turn, row: TraceRow) -> None:
    payload = row.payload
    if row.source != "acp":
        # Only the agent's own Revise reports are read besides the proxy's events.
        _apply_revision(turn, row)
        return
    kind = payload.get("message_kind")
    if row.event_type == _PROMPT and kind == "user":
        prompt = next((block for block in turn.blocks if block["type"] == "prompt"), None)
        if prompt is None or prompt.get("captured"):
            prompt = turn.block("prompt", row.occurred_at)
        value = payload.get("prompt")
        prompt.update(_text_fields(_typed_prompt(value)), captured=True)
        attachments = _prompt_attachments(value)
        if attachments:
            prompt["attachments"] = attachments
        return
    if row.event_type == _PROMPT and kind in ("thought", "assistant"):
        block_type = "thought" if kind == "thought" else "message"
        value = payload.get("reasoning") if kind == "thought" else payload.get("content")
        message_id = payload.get("message_id") or row.correlations.get("message_id")
        text, redacted = content_text(value)
        last = turn.blocks[-1] if turn.blocks else None
        if last is not None and last["type"] == block_type and last.get("_message_id") == message_id:
            merged = (last.get("_raw") or "") + (text or "")
            last["_raw"] = merged
            last["text"], last["truncated"] = _cap(merged or None)
            last["redacted"] = last["redacted"] or redacted
            last["chunks"] += 1
            return
        capped, truncated = _cap(text)
        turn.block(
            block_type,
            row.occurred_at,
            text=capped,
            redacted=redacted,
            truncated=truncated,
            chunks=1,
            _message_id=message_id,
            _raw=text or "",
        )
        return
    if row.event_type in _TOOL_EVENTS:
        call_id = payload.get("tool_call_id") or row.correlations.get("tool_call_id") or row.event_id
        tool = turn.tools.get(call_id)
        if tool is None:
            tool = turn.tools[call_id] = turn.block(
                "tool",
                row.occurred_at,
                tool_call_id=call_id,
                title=None,
                tool_kind=None,
                status=None,
                arguments=None,
                result=None,
                diffs=[],
                started_at=row.occurred_at,
                completed_at=None,
                duration_ms=None,
                permission=None,
            )
        for source_key, target_key in (("tool_name", "title"), ("tool_kind", "tool_kind")):
            if payload.get(source_key):
                tool[target_key] = payload[source_key]
        if row.event_type == "tool.failed":
            tool["status"] = "failed"
        elif payload.get("status"):
            tool["status"] = payload["status"]
        if "arguments" in payload:
            tool["arguments"] = _text_fields(payload["arguments"])
        if "content" in payload:
            tool["result"] = _text_fields(payload["content"])
            diffs = diff_blocks(payload["content"])
            if diffs:
                tool["diffs"] = diffs
        if row.event_type in ("tool.completed", "tool.failed"):
            tool["completed_at"] = row.occurred_at
            tool["duration_ms"] = _ms(tool["started_at"], row.occurred_at)
        return
    if row.event_type == "permission.requested":
        permission = {
            "permission_id": row.correlations.get("permission_id"),
            "requested_at": row.occurred_at,
            "options": [dict(option) for option in payload.get("options") or [] if isinstance(option, Mapping)],
            "decision": None,
            "selected_option_id": None,
            "decided_at": None,
            "wait_ms": None,
        }
        call_id = payload.get("tool_call_id") or row.correlations.get("tool_call_id")
        tool = turn.tools.get(call_id) if call_id else None
        if tool is not None:
            tool["permission"] = permission
        else:
            turn.block("permission", row.occurred_at, permission=permission)
        if permission["permission_id"] is not None:
            turn.permissions[str(permission["permission_id"])] = permission
        return
    if row.event_type == "permission.decided":
        permission_id = row.correlations.get("permission_id")
        permission = turn.permissions.get(str(permission_id)) if permission_id is not None else None
        if permission is None:
            permission = {"permission_id": permission_id, "requested_at": None, "options": []}
            turn.block("permission", row.occurred_at, permission=permission)
        permission.update(
            decision=_decision(payload),
            selected_option_id=payload.get("selected_option_id"),
            decided_at=row.occurred_at,
            wait_ms=_ms(permission.get("requested_at"), row.occurred_at),
        )
        return
    if row.event_type == _COMPLETION:
        turn.completed_at = row.occurred_at
        turn.stop_reason = payload.get("stop_reason")
        usage = row.metrics.get("usage_tokens")
        if isinstance(usage, int) and not isinstance(usage, bool):
            turn.usage_tokens = usage
        return
    if row.event_type == "plan.updated":
        counts = payload.get("plan_status_counts") or []
        completed = sum(
            int(entry.get("count") or 0)
            for entry in counts
            if isinstance(entry, Mapping) and entry.get("status") == "completed"
        )
        turn.block("plan", row.occurred_at, plan_size=payload.get("plan_size"), completed=completed)
        return
    if row.event_type == "interaction.completed":
        method, reason = payload.get(ACP_METHOD_KEY), payload.get(END_REASON_KEY)
        if (row.lifecycle_state or None) in (None, "cancelled") and method in (None, CANCEL_METHOD) and not reason:
            turn.block("cancel", row.occurred_at)
        else:
            turn.block("lifecycle", row.occurred_at, acp_method=method, end_reason=reason)
        return
    if row.event_type == "interaction.started":
        turn.block("lifecycle", row.occurred_at, acp_method=payload.get(ACP_METHOD_KEY), end_reason=None)
        return
    if row.event_type in _ERROR_EVENTS:
        message, _ = content_text(payload.get("error_message"))
        turn.block("error", row.occurred_at, error_code=payload.get("error_code"), message=_cap(message)[0])
        return
    if row.event_type in ("usage.updated", "unknown_source_event"):
        # Unmapped protocol messages carry nothing a reader could use.
        return
    turn.block("event", row.occurred_at, event_type=row.event_type)


def build_page(
    rows: Iterable[TraceRow],
    *,
    limit: int,
    start_index: int = 0,
    max_events: int = MAX_PAGE_EVENTS,
) -> tuple[list[dict[str, Any]], Optional[TraceRow], int]:
    """Up to ``limit`` turns from ``rows``.

    Returns the serialized turns, the first row of the next page (``None`` at
    the end) and the turn index the next page starts from. ``start_index`` is
    the number of turns before this page, so turn numbers continue.
    """
    turns: list[_Turn] = []
    current: Optional[_Turn] = None
    index = start_index
    events = 0
    prompts_on_page = 0
    for row in rows:
        starts_turn = _is_turn_start(row)
        if starts_turn and prompts_on_page >= limit:
            return [turn.serialize() for turn in turns], row, index
        if events >= max_events:
            if current is not None:
                current.continues = True
            return [turn.serialize() for turn in turns], row, index
        events += 1
        if starts_turn:
            index += 1
            prompts_on_page += 1
            current = _Turn(
                index=index,
                kind="turn",
                started_at=row.occurred_at,
                turn_id=row.correlations.get("turn_id"),
                research_session_id=row.research_session_id,
            )
            current.block("prompt", row.occurred_at, text=None, redacted=False, truncated=False, captured=False)
            turns.append(current)
            continue
        if current is None:
            # Before the page's first prompt: the chat opening, or the rest of
            # a turn that began on the previous page.
            current = _Turn(
                index=index,
                kind="preamble" if index == 0 else "continued",
                started_at=row.occurred_at,
                research_session_id=row.research_session_id,
            )
            turns.append(current)
        _apply(current, row)
    return [turn.serialize() for turn in turns], None, index
