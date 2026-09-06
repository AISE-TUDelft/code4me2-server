"""Telemetry record captured per LLM inference call intercepted by the proxy.

One ``InferenceRecord`` maps onto one ``agent_event`` row with
``event_type='model_call'``. Every field is a typed column — this is the
"columnar core" half of the merged schema; adapter-specific extras that don't
warrant a column go into ``agent_event.extra_json`` instead.

Content fields (``first_system_message``, ``last_user_message``,
``response_text``) are only ever populated when the requesting user's
``store_agent_content`` preference resolves True, decided server-side.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


class InferenceRecord(BaseModel):
    request_id: str = Field(
        description="UUID generated per call. Used as the model_call span_id."
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Code4Me session identifier, resolved server-side from the "
        "session cookie. Links all inference calls made within one IDE session.",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="Agent task identifier. Links this call to an AgentTask row "
        "so all inference steps of one task are grouped.",
    )
    agent_profile: Optional[str] = Field(
        default=None,
        description="Profile name assigned for this task (e.g. 'default-goose'). "
        "Allows per-arm analysis of inference behaviour.",
    )
    event_type: str = Field(
        default="model_call",
        description="Event type for the agent_event table. Always 'model_call' "
        "for proxy-captured inference calls.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the request was received by the relay.",
    )
    model: str = Field(
        description="Model actually sent upstream, after the server-side override "
        "from the task's snapshotted profile."
    )
    streaming: bool = Field(
        description="Whether the client requested a streaming response."
    )
    message_count: int = Field(
        description="Total messages in the conversation context sent to the model. "
        "Indicates how deep into a task this call occurred."
    )
    role_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="Count of messages per role (system, user, assistant, tool). "
        "Captures the shape of the context window.",
    )
    first_system_message: Optional[str] = Field(
        default=None,
        description="Text of the system-role message. Content — gated by "
        "store_agent_content.",
    )
    last_user_message: Optional[str] = Field(
        default=None,
        description="Text of the most recent user-role message. Content — gated by "
        "store_agent_content.",
    )
    tools_kept: int = Field(
        default=0,
        description="Number of tool definitions forwarded to the upstream model.",
    )
    tools_stripped: int = Field(
        default=0,
        description="Number of tool definitions removed before forwarding, either "
        "because the profile's allowlist excluded them or the provider rejects "
        "their schema. A non-zero value here is how you discover a tool name "
        "missing from agents.tools.KNOWN_AGENT_TOOLS.",
    )
    tool_names_requested: list[str] = Field(
        default_factory=list,
        description="Function names of all tools in the client request before "
        "filtering. Records what the agent had available regardless of which were "
        "forwarded upstream.",
    )
    max_tokens: Optional[int] = Field(
        default=None, description="max_tokens parameter from the client request."
    )
    active_file: Optional[str] = Field(
        default=None,
        description="Path of the file active in the IDE editor when this call was "
        "made, supplied by the plugin via enrichment. Structural metadata — "
        "stored regardless of the content-storage preference.",
    )
    prompt_tokens: Optional[int] = Field(
        default=None,
        description="Input token count as reported by the upstream provider. None "
        "for streaming calls when the provider returns no usage chunk.",
    )
    completion_tokens: Optional[int] = Field(
        default=None, description="Output token count as reported by the provider."
    )
    total_tokens: Optional[int] = Field(
        default=None, description="Total tokens as reported by the provider."
    )
    finish_reason: Optional[str] = Field(
        default=None,
        description="Why the model stopped ('stop', 'tool_calls', 'length', or "
        "'stream_aborted' when the relay lost the connection mid-flight). Useful "
        "for detecting truncated responses.",
    )
    response_text: Optional[str] = Field(
        default=None,
        description="Text of the model's response. Content — gated by "
        "store_agent_content.",
    )
    latency_ms: int = Field(
        description="End-to-end relay latency in ms (request received → full "
        "response returned). For streaming calls, until the last chunk is flushed."
    )
    upstream_status: int = Field(
        description="HTTP status code returned by the upstream provider."
    )
