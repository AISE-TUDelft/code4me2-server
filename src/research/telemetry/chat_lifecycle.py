"""Chat-lifecycle and Revise telemetry contract shared by producers and readers.

The proxy-side ACP normalizer (:mod:`research.telemetry.normalization.generic_acp`),
the participant proxy and the study analytics read model all use these literals,
so they stay additive: renaming one breaks already-stored events.

The payload keys are chosen so the privacy classifier keeps them as metadata
(``acp_method``, ``end_reason`` and ``selected_option_id`` are BEHAVIORAL; keys
ending in ``_count`` are SYSTEM); an unrecognised key would be classed CONTENT
and redacted under a metadata-only study.

No new canonical event type is involved: a chat start is an
``interaction.started`` and a chat end an ``interaction.completed`` with
lifecycle ``completed``. A user interrupt (``session/cancel``) stays an
``interaction.completed`` without a lifecycle state, which is how analytics keeps
chat ends out of the cancel count.
"""

from __future__ import annotations

__all__ = [
    "ACP_METHOD_KEY",
    "CANCEL_METHOD",
    "CHAT_END_METHODS",
    "CHAT_START_METHODS",
    "END_REASON_AGENT_EXITED",
    "END_REASON_HOST_CLOSED",
    "END_REASON_KEY",
    "END_REASON_SESSION_STALE",
    "END_REASON_SIGNAL_TERMINATED",
    "PROXY_END_REASONS",
    "REVISED_DECISION",
    "REVISE_OPTION_ID",
    "SELECTED_OPTION_ID_KEY",
]

#: Payload key naming the ACP method an event was derived from.
ACP_METHOD_KEY = "acp_method"

#: Payload key on the proxy's final event saying why the chat's process ended.
END_REASON_KEY = "end_reason"

#: Payload key on ``permission.decided`` carrying the option the user picked.
SELECTED_OPTION_ID_KEY = "selected_option_id"

#: ACP methods that start or reattach to a chat, mapped to the start kind
#: analytics reports.
CHAT_START_METHODS: dict[str, str] = {
    "session/new": "new",
    "session/fork": "fork",
    "session/load": "load",
    "session/resume": "resume",
}

#: ACP methods with which the client ends a chat.
CHAT_END_METHODS = frozenset({"session/close", "session/delete"})

#: The ACP user interrupt; its ``interaction.completed`` is the only cancel.
CANCEL_METHOD = "session/cancel"

#: The host closed the proxy's stdin (IntelliJ releases a chat's agent process
#: when the chat is deleted from its history, and on IDE shutdown).
END_REASON_HOST_CLOSED = "host_closed"
#: The agent process exited on its own.
END_REASON_AGENT_EXITED = "agent_exited"
#: The proxy stopped because its research session went stale.
END_REASON_SESSION_STALE = "session_stale"
#: The proxy received SIGTERM/SIGINT.
END_REASON_SIGNAL_TERMINATED = "signal_terminated"

PROXY_END_REASONS = frozenset(
    {
        END_REASON_HOST_CLOSED,
        END_REASON_AGENT_EXITED,
        END_REASON_SESSION_STALE,
        END_REASON_SIGNAL_TERMINATED,
    }
)

#: The permission option id the built-in agent offers for "Revise…". The proxy
#: records it as ``selected_option_id`` on a reject-kind decision.
REVISE_OPTION_ID = "revise"

#: The decision the built-in agent self-reports for an accepted revision form.
REVISED_DECISION = "revised"
