"""Built-in ACP source normalizer: observations into canonical concepts.

This module hosts the built-in, pure ACP normalizer:

* :class:`GenericAcpNormalizer` - ACP JSON-RPC messages. It maps **only
  documented, stable constructs**. It is not a copy of the ACP method
  vocabulary: a method/notification becomes one of the canonical
  :class:`~research.telemetry.enums.CanonicalEventType` concepts, and an unknown
  method becomes ``unknown_source_event`` carrying sanitized metadata (method
  name and parameter *names*, never values) with a ``NEEDS_REVIEW`` coverage
  state.

Vendor-specific semantic enrichment is intentionally out of scope for the ACP
normalizer; an :class:`AgentAdapter` may enrich a candidate later without
deleting or replacing the generic observation. IDE canonicalization belongs to
the IntelliJ plugin, not the server.

Correlation note: ACP is a request/response protocol whose updates arrive as
notifications. Two canonical facts therefore need *stream* context, not a
single frame:

* ``permission.decided`` is derived from the **host response** to a previously
  observed ``session/request_permission`` (the response only carries the
  selected option id, so the request's options are needed to resolve the option
  *kind*);
* ``agent.message.completed`` is derived from the response to a previously
  observed ``session/prompt`` request.

Those correlations (and the "``tool.created`` once per tool call" rule) are held
as bounded, per-session state on the normalizer instance. Every other mapping is
a pure function of one message. The state never changes an observation's
identity, order, or fidelity.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

from ..enums import CanonicalEventType, CanonicalFidelity, CoverageState, EventSource
from ..models import Correlations, Coverage, EventMetrics
from .models import CanonicalCandidateV1, NormalizationResultV1

GENERIC_ACP_NORMALIZER_VERSION = "generic-acp-v1"

#: Canonical ACP permission option kinds that mean "allow".
_ALLOW_OPTION_KINDS = frozenset({"allow_once", "allow_always"})
#: Canonical ACP permission option kinds that mean "reject".
_REJECT_OPTION_KINDS = frozenset({"reject_once", "reject_always"})

_METHOD_RULES: dict[str, tuple[str, CanonicalEventType]] = {
    "initialize": ("acp.initialize", CanonicalEventType.INTERACTION_STARTED),
    "session/new": ("acp.session.new", CanonicalEventType.INTERACTION_STARTED),
    "session/load": ("acp.session.load", CanonicalEventType.INTERACTION_STARTED),
    "session/prompt": ("acp.session.prompt", CanonicalEventType.AGENT_MESSAGE_STARTED),
    "session/cancel": ("acp.session.cancel", CanonicalEventType.INTERACTION_COMPLETED),
    "fs/read_text_file": ("acp.fs.read_text_file", CanonicalEventType.IDE_FILE_OPENED),
    "fs/write_text_file": ("acp.fs.write_text_file", CanonicalEventType.IDE_FILE_SAVED),
    "terminal/create": ("acp.terminal.create", CanonicalEventType.TOOL_STARTED),
}


def _candidate(
    event_type: CanonicalEventType,
    rule_id: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    lifecycle_state: Optional[str] = None,
    coverage: Optional[Coverage] = None,
    correlations: Optional[Correlations] = None,
    metrics: Optional[EventMetrics] = None,
) -> CanonicalCandidateV1:
    return CanonicalCandidateV1(
        event_type=event_type,
        fidelity=CanonicalFidelity.NORMALIZED,
        mapping_rule_id=rule_id,
        payload=payload or {},
        correlations=correlations or Correlations(),
        lifecycle_state=lifecycle_state,
        coverage=coverage or Coverage(state=CoverageState.AVAILABLE),
        metrics=metrics or EventMetrics(),
    )


def _with_turn_id(
    candidates: list[CanonicalCandidateV1], turn_id: str
) -> list[CanonicalCandidateV1]:
    """Attach [turn_id] to candidates that carry no turn of their own.

    Additive only: a candidate whose mapping already set a turn is never
    overwritten. Events are frozen, so the stamp is a copy.
    """
    stamped: list[CanonicalCandidateV1] = []
    for candidate in candidates:
        correlations = candidate.correlations
        if correlations.turn_id is not None:
            stamped.append(candidate)
            continue
        stamped.append(
            candidate.model_copy(
                update={
                    "correlations": correlations.model_copy(
                        update={"turn_id": turn_id}
                    )
                }
            )
        )
    return stamped


class AgentAdapter(Protocol):
    """Optional vendor adapter that enriches (never replaces) generic events."""

    adapter_version: str
    mapping_rule_version: str
    supported_release_ranges: list[str]

    def enrich(
        self, candidate: CanonicalCandidateV1
    ) -> Optional[CanonicalCandidateV1]:  # pragma: no cover - structural protocol
        ...


def enrich_with_adapter(
    candidate: CanonicalCandidateV1, adapter: AgentAdapter
) -> CanonicalCandidateV1:
    """Apply an adapter without deleting or replacing generic fields.

    The generic candidate's payload wins on conflict, so an adapter can only add
    fields; it can never erase or rewrite the source observation.
    """
    enriched = adapter.enrich(candidate)
    if enriched is None:
        return candidate
    merged_payload = {**enriched.payload, **candidate.payload}
    return candidate.model_copy(
        update={"payload": merged_payload, "adapter_version": adapter.adapter_version}
    )


def _unknown_candidate(
    method: Optional[str], params: Any, *, source_event_id: Optional[str] = None
) -> CanonicalCandidateV1:
    payload: dict[str, Any] = {}
    if method is not None:
        payload["unknown_method"] = method
    if isinstance(params, Mapping):
        # Parameter *names* only; values may be sensitive.
        payload["param_names"] = sorted(str(key) for key in params)
    if source_event_id is not None:
        payload["source_event_id"] = source_event_id
    return _candidate(
        CanonicalEventType.UNKNOWN_SOURCE_EVENT,
        "acp.unknown",
        payload=payload,
        coverage=Coverage(
            state=CoverageState.NEEDS_REVIEW,
            reason="unrecognized ACP construct preserved as metadata only",
        ),
    )


def _tool_payload(location: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source_key, target_key in (
        ("toolCallId", "tool_call_id"),
        ("title", "tool_name"),
        ("kind", "tool_kind"),
        ("status", "status"),
    ):
        value = location.get(source_key)
        if value is not None:
            payload[target_key] = value
    raw_input = location.get("rawInput")
    if raw_input is not None:
        payload["arguments"] = raw_input
    content = location.get("content")
    if content is not None:
        payload["content"] = content
    return payload


def _tool_call_id(update: Mapping[str, Any]) -> Optional[str]:
    value = update.get("toolCallId")
    return value if isinstance(value, str) and value else None


def _status(update: Mapping[str, Any]) -> str:
    return str(update.get("status") or "").lower()


def _terminal_tool_candidate(
    update: Mapping[str, Any],
) -> Optional[CanonicalCandidateV1]:
    """Map a terminal ``tool_call``/``tool_call_update`` status; else ``None``."""
    status = _status(update)
    if status in {"completed", "success", "succeeded"}:
        return _candidate(
            CanonicalEventType.TOOL_COMPLETED,
            "acp.update.tool_call_update.completed",
            payload=_tool_payload(update),
            lifecycle_state="completed",
        )
    if status in {"failed", "error"}:
        return _candidate(
            CanonicalEventType.TOOL_FAILED,
            "acp.update.tool_call_update.failed",
            payload=_tool_payload(update),
            lifecycle_state="failed",
        )
    return None


def _first_int(source: Mapping[str, Any], keys: tuple[str, ...]) -> Optional[int]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
    return None


def _usage_metrics(source: Any) -> EventMetrics:
    """Build usage metrics from an agent-reported usage mapping.

    A reported total (or a derivable input+output total) is an observed value
    with ``AVAILABLE`` coverage - including an observed ``0``. When the agent
    reports no token counts at all, the value stays ``None`` with ``UNAVAILABLE``
    coverage; a missing measurement is never coerced to zero.
    """
    tokens: Optional[int] = None
    if isinstance(source, Mapping):
        tokens = _first_int(source, ("totalTokens", "total_tokens"))
        if tokens is None:
            input_tokens = _first_int(source, ("inputTokens", "input_tokens"))
            output_tokens = _first_int(source, ("outputTokens", "output_tokens"))
            if input_tokens is not None or output_tokens is not None:
                tokens = (input_tokens or 0) + (output_tokens or 0)
        if tokens is None:
            tokens = _first_int(source, ("used",))
    if tokens is None:
        return EventMetrics(
            usage_tokens=None,
            usage_capability=Coverage(
                state=CoverageState.UNAVAILABLE,
                reason="agent did not report token usage",
                capability="usage",
            ),
        )
    return EventMetrics(
        usage_tokens=tokens,
        usage_capability=Coverage(state=CoverageState.AVAILABLE, capability="usage"),
    )


def _decision_for(option_id: str, kind: Optional[str]) -> str:
    """Resolve an allow/reject/cancelled decision from a selected option."""
    if kind in _ALLOW_OPTION_KINDS:
        return "allow"
    if kind in _REJECT_OPTION_KINDS:
        return "reject"
    text = option_id.lower()
    if "reject" in text or "deny" in text:
        return "reject"
    if "allow" in text or "approve" in text or "accept" in text:
        return "allow"
    return "selected"


def _normalized_options(
    params: Any,
) -> tuple[list[dict[str, str]], dict[str, Optional[str]]]:
    """Return ``(payload_options, {option_id: kind})`` from request params."""
    payload_options: list[dict[str, str]] = []
    option_kinds: dict[str, Optional[str]] = {}
    if not isinstance(params, Mapping):
        return payload_options, option_kinds
    raw_options = params.get("options")
    if not isinstance(raw_options, list):
        return payload_options, option_kinds
    for option in raw_options:
        if not isinstance(option, Mapping):
            continue
        option_id = option.get("optionId")
        if not isinstance(option_id, str):
            option_id = option.get("option_id")
        kind = option.get("kind")
        entry: dict[str, str] = {}
        if isinstance(option_id, str) and option_id:
            entry["option_id"] = option_id
            option_kinds[option_id] = kind if isinstance(kind, str) else None
        if isinstance(kind, str):
            entry["kind"] = kind
        if entry:
            payload_options.append(entry)
    return payload_options, option_kinds


def _tool_call_request_tool_id(params: Any) -> Optional[str]:
    if not isinstance(params, Mapping):
        return None
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, Mapping):
        return None
    value = tool_call.get("toolCallId")
    return value if isinstance(value, str) and value else None


class GenericAcpNormalizer:
    """Maps ACP JSON-RPC messages to canonical candidates.

    A normalizer instance is bound to one observer stream (session): it keeps
    only the correlation state required for ``permission.decided`` /
    ``agent.message.completed`` and for emitting ``tool.created`` exactly once
    per tool call.
    """

    normalizer_version = GENERIC_ACP_NORMALIZER_VERSION

    def __init__(self) -> None:
        #: jsonrpc permission request id -> {"options": {optionId: kind}, "tool_call_id": ...}
        self._pending_permissions: dict[str, dict[str, Any]] = {}
        #: jsonrpc ``session/prompt`` request id -> ACP session key awaiting its response.
        self._pending_prompts: dict[str, str] = {}
        #: ACP session key -> the native prompt request id of its active turn.
        self._current_turn: dict[str, str] = {}
        #: in-flight request id -> the turn it belongs to (settled by its response).
        self._request_turns: dict[str, str] = {}
        #: tool call ids for which ``tool.created`` was already emitted.
        self._created_tool_calls: set[str] = set()

    @staticmethod
    def _session_key(message: Mapping[str, Any]) -> Optional[str]:
        """The ACP session id carried by a frame, if any.

        It keys turn state when one stream multiplexes several ACP sessions. A
        response may omit it; responses are matched by request id instead.
        """
        for container in ("params", "result"):
            section = message.get(container)
            if isinstance(section, Mapping):
                session_id = section.get("sessionId")
                if isinstance(session_id, str) and session_id:
                    return session_id
        return None

    def _current_turn_for(self, message: Mapping[str, Any]) -> Optional[str]:
        """The active turn for this frame's session, or ``None`` when none.

        A frame without an explicit session association never inherits a turn:
        no turn id is invented when no prompt is known to be pending.
        """
        session_key = self._session_key(message)
        if session_key is None:
            return None
        return self._current_turn.get(session_key)

    def _advance_turn_state(self, message: Any) -> Optional[str]:
        """Update bounded turn state and return this frame's native turn id.

        The turn id is the JSON-RPC id of the ``session/prompt`` request. It is
        returned for the prompt itself, for frames observed while it is pending,
        and for its response; ``None`` means no prompt is pending.
        """
        if not isinstance(message, Mapping):
            return None
        method = message.get("method")
        message_id = message.get("id")
        request_id = str(message_id) if message_id is not None else None

        if method == "session/prompt" and request_id is not None:
            session_key = self._session_key(message) or ""
            self._current_turn[session_key] = request_id
            self._pending_prompts[request_id] = session_key
            return request_id

        if method is None and request_id is not None:
            # A response settles any tracked sub-request, else closes the prompt.
            settled = self._request_turns.pop(request_id, None)
            if settled is not None:
                return settled
            if request_id in self._pending_prompts:
                return request_id
            return None

        if method is None:
            return None

        turn_id = self._current_turn_for(message)
        if turn_id is not None and request_id is not None:
            self._request_turns[request_id] = turn_id
        return turn_id

    def _close_turn(self, prompt_id: str, session_key: str) -> None:
        """Clear the prompt's active turn and any outstanding sub-requests."""
        if self._current_turn.get(session_key) == prompt_id:
            del self._current_turn[session_key]
        for request_id in [
            key for key, turn in self._request_turns.items() if turn == prompt_id
        ]:
            del self._request_turns[request_id]

    def normalize(
        self,
        message: Any,
        *,
        source_event_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> NormalizationResultV1:
        """Normalize one ACP message and attach its native turn correlation.

        Turn state advances before mapping: the ``session/prompt`` request opens
        a turn, frames observed while it is pending inherit its id, and the
        prompt response closes it. State is per-stream (this instance), keyed by
        ACP session, so concurrent sessions never share a turn.
        """
        turn_id = self._advance_turn_state(message)
        result = self._normalize_message(
            message,
            source_event_id=source_event_id,
            direction=direction,
        )
        if turn_id is None:
            return result
        return result.model_copy(
            update={"candidates": _with_turn_id(result.candidates, turn_id)}
        )

    def _normalize_message(
        self,
        message: Any,
        *,
        source_event_id: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> NormalizationResultV1:
        """Map one ACP message (zero or more candidates) to canonical concepts.

        ``direction`` is the optional ``host_to_agent`` / ``agent_to_host`` frame
        direction. When provided it disambiguates responses (a permission
        decision is only taken from a host response; a message completion only
        from an agent response); when omitted, correlation falls back to the
        JSON-RPC id alone.
        """
        if not isinstance(message, Mapping):
            return NormalizationResultV1(
                source=EventSource.ACP,
                normalizer_version=self.normalizer_version,
                source_event_id=source_event_id,
                candidates=[],
                unmapped_reason="message is not a JSON object",
            )

        method = message.get("method")
        params = message.get("params")
        message_id = message.get("id")
        resolved_source_id = (
            source_event_id
            if source_event_id is not None
            else (str(message_id) if message_id is not None else None)
        )

        if method == "session/update" and isinstance(params, Mapping):
            update = params.get("update")
            if isinstance(update, Mapping):
                return NormalizationResultV1(
                    source=EventSource.ACP,
                    normalizer_version=self.normalizer_version,
                    source_event_id=resolved_source_id,
                    candidates=self._update_candidates(update),
                )

        if method == "session/request_permission":
            return NormalizationResultV1(
                source=EventSource.ACP,
                normalizer_version=self.normalizer_version,
                source_event_id=resolved_source_id,
                candidates=[self._permission_request(message_id, params)],
            )

        if isinstance(method, str) and method in _METHOD_RULES:
            rule_id, event_type = _METHOD_RULES[method]
            payload: dict[str, Any] = {}
            if isinstance(params, Mapping):
                session_id = params.get("sessionId")
                if isinstance(session_id, str):
                    payload["session_id"] = session_id
            return NormalizationResultV1(
                source=EventSource.ACP,
                normalizer_version=self.normalizer_version,
                source_event_id=resolved_source_id,
                candidates=[_candidate(event_type, rule_id, payload=payload)],
            )

        if "error" in message:
            return NormalizationResultV1(
                source=EventSource.ACP,
                normalizer_version=self.normalizer_version,
                source_event_id=resolved_source_id,
                candidates=[self._agent_error(message, direction)],
            )

        # A response carries no method; pair it with an observed request.
        if method is None and message_id is not None:
            response = self._response_candidate(str(message_id), message, direction)
            if response is not None:
                return NormalizationResultV1(
                    source=EventSource.ACP,
                    normalizer_version=self.normalizer_version,
                    source_event_id=resolved_source_id,
                    candidates=[response],
                )

        return NormalizationResultV1(
            source=EventSource.ACP,
            normalizer_version=self.normalizer_version,
            source_event_id=resolved_source_id,
            candidates=[_unknown_candidate(method, params, source_event_id=resolved_source_id)],
        )

    # -- session/update ----------------------------------------------------

    def _update_candidates(
        self, update: Mapping[str, Any]
    ) -> list[CanonicalCandidateV1]:
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            payload: dict[str, Any] = {"message_kind": "assistant"}
            if isinstance(update.get("messageId"), str):
                payload["message_id"] = update["messageId"]
            if update.get("content") is not None:
                payload["content"] = update["content"]
            return [
                _candidate(
                    CanonicalEventType.AGENT_MESSAGE_STARTED,
                    "acp.update.agent_message_chunk",
                    payload=payload,
                    lifecycle_state="started",
                )
            ]

        if kind == "agent_thought_chunk":
            payload = {"message_kind": "thought"}
            if isinstance(update.get("messageId"), str):
                payload["message_id"] = update["messageId"]
            if update.get("content") is not None:
                # Reasoning is CONTENT and prohibited by default; it is only kept
                # when content capture is explicitly allowed AND consented.
                payload["reasoning"] = update["content"]
            return [
                _candidate(
                    CanonicalEventType.AGENT_MESSAGE_STARTED,
                    "acp.update.agent_thought_chunk",
                    payload=payload,
                    lifecycle_state="started",
                )
            ]

        if kind == "tool_call":
            return self._tool_call_candidates(update)

        if kind == "tool_call_update":
            terminal = _terminal_tool_candidate(update)
            if terminal is not None:
                return [terminal]
            return [
                _candidate(
                    CanonicalEventType.TOOL_STARTED,
                    "acp.update.tool_call_update",
                    payload=_tool_payload(update),
                    lifecycle_state="started",
                )
            ]

        if kind == "plan":
            return [self._plan_candidate(update)]

        if kind == "usage_update":
            return [
                _candidate(
                    CanonicalEventType.USAGE_UPDATED,
                    "acp.update.usage_update",
                    metrics=_usage_metrics(update),
                )
            ]

        return [
            _unknown_candidate(
                f"session/update:{kind}" if kind else "session/update", update
            )
        ]

    def _tool_call_candidates(
        self, update: Mapping[str, Any]
    ) -> list[CanonicalCandidateV1]:
        tool_call_id = _tool_call_id(update)
        already_created = (
            tool_call_id is not None and tool_call_id in self._created_tool_calls
        )
        if already_created:
            candidates = [
                _candidate(
                    CanonicalEventType.TOOL_STARTED,
                    "acp.update.tool_call",
                    payload=_tool_payload(update),
                    lifecycle_state="started",
                )
            ]
        else:
            if tool_call_id is not None:
                self._created_tool_calls.add(tool_call_id)
            candidates = [
                _candidate(
                    CanonicalEventType.TOOL_CREATED,
                    "acp.update.tool_call.created",
                    payload=_tool_payload(update),
                    lifecycle_state="pending",
                )
            ]
        # A single-shot ``tool_call`` can already be terminal.
        terminal = _terminal_tool_candidate(update)
        if terminal is not None:
            candidates.append(terminal)
        return candidates

    def _plan_candidate(self, update: Mapping[str, Any]) -> CanonicalCandidateV1:
        payload: dict[str, Any] = {}
        entries = update.get("entries")
        if isinstance(entries, list):
            payload["plan_size"] = len(entries)
            status_counts: dict[str, int] = {}
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                status = entry.get("status")
                if isinstance(status, str):
                    status_counts[status] = status_counts.get(status, 0) + 1
            if status_counts:
                # A list of {"status", "count"} records, not a status-keyed map:
                # the fail-closed classifier treats status strings used as keys
                # as unclassified content and would redact their values.
                payload["plan_status_counts"] = [
                    {"status": status, "count": count}
                    for status, count in sorted(status_counts.items())
                ]
        return _candidate(
            CanonicalEventType.PLAN_UPDATED, "acp.update.plan", payload=payload
        )

    # -- permissions -------------------------------------------------------

    def _permission_request(
        self, message_id: Any, params: Any
    ) -> CanonicalCandidateV1:
        permission_id = str(message_id) if message_id is not None else None
        tool_call_id = _tool_call_request_tool_id(params)
        options, option_kinds = _normalized_options(params)
        payload: dict[str, Any] = {}
        if tool_call_id is not None:
            payload["tool_call_id"] = tool_call_id
        if options:
            payload["options"] = options
            payload["option_count"] = len(options)
        if permission_id is not None:
            self._pending_permissions[permission_id] = {
                "tool_call_id": tool_call_id,
                "options": option_kinds,
            }
        return _candidate(
            CanonicalEventType.PERMISSION_REQUESTED,
            "acp.permission.request",
            payload=payload,
            correlations=Correlations(
                permission_id=permission_id, tool_call_id=tool_call_id
            ),
        )

    def _permission_decided(
        self, permission_id: str, message: Mapping[str, Any]
    ) -> CanonicalCandidateV1:
        spec = self._pending_permissions.pop(permission_id, {})
        tool_call_id = spec.get("tool_call_id")
        option_kinds: Mapping[str, Optional[str]] = spec.get("options") or {}
        result = message.get("result")
        payload: dict[str, Any] = {}
        if isinstance(result, Mapping):
            outcome = result.get("outcome")
            if isinstance(outcome, Mapping):
                selected = outcome.get("outcome")
                if isinstance(selected, str):
                    payload["outcome"] = selected
                option_id = outcome.get("optionId")
                if not isinstance(option_id, str):
                    option_id = outcome.get("option_id")
                if selected == "selected" and isinstance(option_id, str):
                    kind = option_kinds.get(option_id)
                    payload["selected_option_id"] = option_id
                    if isinstance(kind, str):
                        payload["selected_option_kind"] = kind
                    payload["decision"] = _decision_for(option_id, kind)
                elif selected == "cancelled":
                    payload["decision"] = "cancelled"
            elif isinstance(outcome, str):
                # Some ACP revisions surface the outcome string directly.
                payload["outcome"] = outcome
                if outcome == "cancelled":
                    payload["decision"] = "cancelled"
        payload.setdefault("outcome", "unknown")
        payload.setdefault("decision", "unknown")
        return _candidate(
            CanonicalEventType.PERMISSION_DECIDED,
            "acp.permission.decided",
            payload=payload,
            correlations=Correlations(
                permission_id=permission_id, tool_call_id=tool_call_id
            ),
        )

    # -- responses / messages / errors -------------------------------------

    def _response_candidate(
        self, response_id: str, message: Mapping[str, Any], direction: Optional[str]
    ) -> Optional[CanonicalCandidateV1]:
        if response_id in self._pending_permissions and direction in (
            None,
            "host_to_agent",
        ):
            return self._permission_decided(response_id, message)
        if response_id in self._pending_prompts and direction in (None, "agent_to_host"):
            session_key = self._pending_prompts.pop(response_id)
            self._close_turn(response_id, session_key)
            return self._message_completed(message)
        return None

    def _message_completed(self, message: Mapping[str, Any]) -> CanonicalCandidateV1:
        result = message.get("result")
        payload: dict[str, Any] = {}
        usage_source: Any = result
        if isinstance(result, Mapping):
            stop_reason = result.get("stopReason")
            if not isinstance(stop_reason, str):
                stop_reason = result.get("stop_reason")
            if isinstance(stop_reason, str):
                payload["stop_reason"] = stop_reason
            if isinstance(result.get("usage"), Mapping):
                usage_source = result["usage"]
        return _candidate(
            CanonicalEventType.AGENT_MESSAGE_COMPLETED,
            "acp.message.completed",
            payload=payload,
            lifecycle_state="completed",
            metrics=_usage_metrics(usage_source),
        )

    def _agent_error(
        self, message: Mapping[str, Any], direction: Optional[str]
    ) -> CanonicalCandidateV1:
        error = message.get("error")
        # A JSON-RPC error response observed on the wire is normally the agent
        # rejecting a host request; a host->agent error is a host rejection.
        error_source = "host" if direction == "host_to_agent" else "agent"
        payload: dict[str, Any] = {"error_source": error_source}
        if isinstance(error, Mapping):
            if error.get("code") is not None:
                payload["error_code"] = error["code"]
            if error.get("message") is not None:
                payload["error_message"] = error["message"]
        return _candidate(
            CanonicalEventType.AGENT_ERROR, "acp.error_response", payload=payload
        )


__all__ = [
    "AgentAdapter",
    "GENERIC_ACP_NORMALIZER_VERSION",
    "GenericAcpNormalizer",
    "enrich_with_adapter",
]
