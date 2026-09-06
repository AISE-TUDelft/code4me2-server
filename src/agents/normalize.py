"""Wire-format normalization for the agent inference relay.

Third-party agents don't speak one dialect. This module holds everything needed
to turn what they actually send into something a generic OpenAI-compatible
upstream will accept, and to read telemetry back out of the response:

* **Chat Completions** (``messages``) — what Goose and the built-in
  ``code4me2-agent`` runtime send. Needs JSON-Schema sanitisation, because
  Goose's Rust-generated tool schemas carry keywords (``$ref``, ``uint32``
  formats, union ``type`` arrays) that most providers reject outright.
* **Responses API** (``input``) — what Codex sends. OpenAI-proprietary and *not*
  chat-completions-shaped, so per merge decision 6 it stays a special-cased
  path rather than the default: when the target isn't OpenAI itself, the
  proprietary fields have to be stripped or rewritten.

Also here: extracting token counts / finish reason / response text from both
streaming (SSE) and non-streaming replies, and reconstructing the tool calls an
agent executed locally between two inference requests.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

_SNIPPET_LEN = 300


def snippet(text: str) -> str:
    """Collapse text to a single short line for logs and content columns."""
    text = text.strip().replace("\n", " ")
    return text[:_SNIPPET_LEN] + "…" if len(text) > _SNIPPET_LEN else text


# ── Goose-specific prompt cruft ───────────────────────────────────────────────

# Goose prepends an <info-msg>...</info-msg> block (current time, working
# directory, todo notes) to every user message it sends — strip it so telemetry
# reflects the user's actual request, not this injected context.
_INFO_MSG_RE = re.compile(r"<info-msg>.*?</info-msg>", re.DOTALL | re.IGNORECASE)

# Goose also sends separate "generate a chat title" requests, which wrap the
# real user message(s) like:
#   ---BEGIN USER MESSAGES---
#   <actual message>
#   ---END USER MESSAGES---
#   Generate a short title for the above messages.
# Unwrap these so telemetry captures the underlying request, not the title prompt.
_TITLE_GEN_RE = re.compile(
    r"---BEGIN USER MESSAGES---(.*?)---END USER MESSAGES---", re.DOTALL
)


def strip_info_msg(text: str) -> str:
    text = _INFO_MSG_RE.sub("", text)
    title_match = _TITLE_GEN_RE.search(text)
    if title_match:
        text = title_match.group(1)
    return text.strip()


# Goose fires small auxiliary requests alongside the main conversation — e.g. a
# "summarize this tool call" request after each tool execution, and the
# chat-title request matched by _TITLE_GEN_RE above. These have their own tiny,
# unrelated message arrays and must not be mistaken for (or mask) a real new
# chat session.
_META_SYSTEM_SUBSTRINGS = ("summarize this tool call",)


def is_meta_request(current_messages: list) -> bool:
    if not current_messages:
        return False
    for msg in current_messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            continue
        content_lower = content.lower()
        if any(p in content_lower for p in _META_SYSTEM_SUBSTRINGS):
            return True
        if "---begin user messages---" in content_lower:
            return True
    return False


def first_user_message_text(
    messages: list, is_responses_api: bool = False
) -> Optional[str]:
    """Find the first user-role message's text — used as the task description on
    the first inference call of a task, when none was supplied by the plugin."""
    for m in messages:
        if not isinstance(m, dict):
            continue
        if is_responses_api:
            if m.get("type") != "message" or m.get("role") != "user":
                continue
            content = m.get("content", "")
            text = (
                " ".join(
                    c.get("text", "")
                    for c in content
                    if isinstance(c, dict) and c.get("type") == "input_text"
                )
                if isinstance(content, list)
                else str(content)
            )
        else:
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            text = content if isinstance(content, str) else ""
        text = strip_info_msg(text)
        if text:
            return text
    return None


def extract_tool_executions(
    messages: list, is_responses_api: bool = False
) -> list[dict]:
    """Pair tool-call requests with their results from newly-appended messages.

    Third-party agents execute tools locally and append both the call (assistant
    ``tool_calls``) and its result (a ``tool``-role message, matched by
    ``tool_call_id``) to the conversation before the next inference request:

        {"role": "assistant", "tool_calls": [{"id": "fc_...",
            "function": {"name": "tree", "arguments": "{\\"path\\": \\".\\"}"}}]}
        {"role": "tool", "tool_call_id": "fc_...", "content": "src/  [85K]\\n..."}

    That's what lets the proxy record tool_call events without needing a
    reporting hook from inside the agent. The Responses API
    (``function_call``/``function_call_output``) branch follows the same
    item-list shape. Calls without a matching result yet are reported with a
    None result.
    """
    pending: dict[str, dict] = {}
    completed: list[dict] = []

    if is_responses_api:
        for item in messages:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if call_id:
                    pending[call_id] = {
                        "name": item.get("name"),
                        "arguments": item.get("arguments"),
                    }
            elif item.get("type") == "function_call_output":
                call = pending.pop(item.get("call_id"), None)
                output = item.get("output")
                if isinstance(output, list):
                    output = " ".join(
                        c.get("text", "") for c in output if isinstance(c, dict)
                    )
                completed.append(
                    {
                        "name": call.get("name") if call else None,
                        "arguments": call.get("arguments") if call else None,
                        "result": output,
                    }
                )
    else:
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant" and isinstance(m.get("tool_calls"), list):
                for tc in m["tool_calls"]:
                    if not isinstance(tc, dict):
                        continue
                    call_id = tc.get("id")
                    fn = tc.get("function") or {}
                    if call_id:
                        pending[call_id] = {
                            "name": fn.get("name"),
                            "arguments": fn.get("arguments"),
                        }
            elif m.get("role") == "tool":
                call = pending.pop(m.get("tool_call_id"), None)
                content = m.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        c.get("text", "") for c in content if isinstance(c, dict)
                    )
                completed.append(
                    {
                        "name": call.get("name") if call else None,
                        "arguments": call.get("arguments") if call else None,
                        "result": content,
                    }
                )

    # Calls still awaiting a result (e.g. tool failed before producing output).
    for call in pending.values():
        completed.append(
            {
                "name": call.get("name"),
                "arguments": call.get("arguments"),
                "result": None,
            }
        )

    return completed


# ── JSON Schema sanitisation ─────────────────────────────────────────────────
#
# Fields that OpenAI-compatible tool-calling APIs do not support.
#
# References:
#   OpenAI function calling:   https://platform.openai.com/docs/guides/function-calling
#   OpenAI structured outputs: https://platform.openai.com/docs/guides/structured-outputs#supported-schemas
#   Groq tool use:             https://console.groq.com/docs/tool-use
#
# Supported subset (keep these):
#   type (single string), description, properties, required, enum, items,
#   anyOf, minimum/maximum, minLength/maxLength, minItems/maxItems, const,
#   default, pattern
_STRIP_KEYS = frozenset(
    {
        # JSON Schema meta / reference keywords — not part of the OpenAI subset
        # https://json-schema.org/understanding-json-schema/structuring
        "$schema",
        "$ref",
        "$defs",
        "$id",
        "$anchor",
        # "title" is valid JSON Schema but several providers reject it at the
        # parameter level
        "title",
        # Explicitly unsupported by OpenAI structured outputs, and a common
        # cause of provider-side failed_generation errors:
        # https://platform.openai.com/docs/guides/structured-outputs#some-type-specific-keywords-are-not-yet-supported
        "additionalProperties",
        "unevaluatedProperties",
        "patternProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        # Conditional composition keywords — not in the supported subset
        "if",
        "then",
        "else",
        "not",
        # Deprecated OpenAI nullable flag; use anyOf: [{type: T}, {type: "null"}]
        # https://platform.openai.com/docs/guides/structured-outputs#all-fields-must-be-required
        "nullable",
    }
)

# Non-standard numeric format strings providers reject (Goose's Rust-generated
# schemas emit these). The OpenAI spec only recognises "date-time", "email",
# "uri", etc. — not Rust integer widths.
_UNSUPPORTED_FORMATS = frozenset(
    {"uint8", "uint16", "uint32", "uint64", "int32", "int64"}
)


def _inline_refs(params: dict, defs: dict) -> None:
    """Resolve $ref pointers using the $defs map, inlining the definition in-place.

    Only handles local fragment refs of the form "#/$defs/Name". Unknown refs are
    left as-is and will be stripped by ``sanitize_schema`` afterwards.
    """
    if "$ref" in params:
        ref = params.pop("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref[len("#/$defs/") :]
            resolved = defs.get(name)
            if isinstance(resolved, dict):
                params.update({k: v for k, v in resolved.items() if k not in params})

    for child in params.get("properties", {}).values():
        if isinstance(child, dict):
            _inline_refs(child, defs)

    for schema_list_key in ("anyOf", "oneOf", "allOf"):
        for child in params.get(schema_list_key, []):
            if isinstance(child, dict):
                _inline_refs(child, defs)

    items = params.get("items")
    if isinstance(items, dict):
        _inline_refs(items, defs)


def sanitize_schema(params: dict) -> None:
    """Resolve $ref/$defs, then recursively strip JSON Schema fields providers reject.

    Mutates ``params`` in place. Supported subset: type (string), description,
    properties, required, enum, items, anyOf, minimum/maximum,
    minLength/maxLength, minItems/maxItems, const, default, pattern.
    """
    # Inline $ref references before stripping $defs so no information is lost —
    # e.g. Goose's extensionmanager__manage_extensions.action is a $ref to an
    # enum in $defs, and dropping it unresolved would lose the allowed values.
    defs = params.get("$defs", {})
    if defs:
        _inline_refs(params, defs)

    for key in _STRIP_KEYS:
        params.pop(key, None)

    fmt = params.get("format")
    if isinstance(fmt, str) and fmt in _UNSUPPORTED_FORMATS:
        params.pop("format")

    if isinstance(params.get("type"), list):
        types = params["type"]
        non_null = [t for t in types if t != "null"]
        if len(non_null) == 1:
            params["type"] = non_null[0]
            if params.get("default") is None and "default" in params:
                params.pop("default")

    for child in params.get("properties", {}).values():
        if isinstance(child, dict):
            sanitize_schema(child)

    items = params.get("items")
    if isinstance(items, dict):
        sanitize_schema(items)

    for schema_list_key in ("anyOf", "oneOf", "allOf"):
        for child in params.get(schema_list_key, []):
            if isinstance(child, dict):
                sanitize_schema(child)


# ── Responses API (Codex) normalization ──────────────────────────────────────

# Top-level Responses API fields that are OpenAI-proprietary and rejected by
# other providers. Codex always sends these; strip them before forwarding to a
# non-OpenAI upstream.
_RESPONSES_API_STRIP_KEYS = frozenset(
    {
        "reasoning",  # o-series reasoning effort config
        "include",  # e.g. ["reasoning.encrypted_content"]
        "text",  # verbosity / format config
        "prompt_cache_key",  # OpenAI prompt caching
        "store",  # OpenAI storage flag
        "client_metadata",  # Codex internal telemetry
        "service_tier",  # OpenAI tier routing (e.g. "priority")
        "parallel_tool_calls",  # Chat Completions concept — not in the Responses spec
        # Note: `instructions` is handled separately below — it's injected as a
        # system message into the input array rather than simply dropped.
    }
)

_VALID_REASONING_EFFORTS = frozenset({"low", "medium", "high"})


def normalize_responses_api_body(body: dict) -> None:
    """Rewrite a Codex Responses-API body so a generic provider will accept it.

    Only called when the upstream is *not* OpenAI (see
    ``reconcile_openai_passthrough`` for that case). Mutates ``body`` in place:

    * drops OpenAI-proprietary top-level fields;
    * moves ``instructions`` into the input array as a system message, since
      it's an OpenAI-only top-level field but the content still matters;
    * rewrites the OpenAI-only ``developer`` role to ``system``;
    * coerces every tool to ``type="function"``. Codex also sends
      ``type="custom"`` freeform-grammar tools (apply_patch, exec_command, …)
      and hosted tools (web_search, …), which generic providers don't
      implement — exposing them as plain functions with a single ``input``
      string keeps them callable, and Codex still executes the result locally.
    """
    for key in _RESPONSES_API_STRIP_KEYS:
        body.pop(key, None)

    instructions = body.pop("instructions", None)
    if instructions:
        input_items = body.get("input", [])
        if isinstance(input_items, list):
            input_items.insert(
                0,
                {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": instructions}],
                },
            )
            body["input"] = input_items

    for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
        if isinstance(item, dict) and item.get("role") == "developer":
            item["role"] = "system"

    raw_tools = body.get("tools")
    if isinstance(raw_tools, list):
        sanitized = []
        for t in raw_tools:
            if not isinstance(t, dict) or not t.get("name"):
                continue  # tools without a name are unusable — drop silently
            if t.get("type") == "function":
                params = t.get("parameters", {})
                sanitize_schema(params)
                sanitized.append(
                    {
                        "type": "function",
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": params,
                    }
                )
            else:
                sanitized.append(
                    {
                        "type": "function",
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": {
                            "type": "object",
                            "properties": {"input": {"type": "string"}},
                            "required": ["input"],
                        },
                    }
                )
        body["tools"] = sanitized
    body.pop("tool_choice", None)


def reconcile_openai_passthrough(body: dict, model: str) -> None:
    """Fix up a Responses-API body that *is* going to OpenAI, for a pinned model.

    When the server overrides Codex's requested model, parameters Codex chose
    for its original model can become invalid for ours, 400-ing an otherwise
    fine request. Reconcile the two known cases:

    * ``gpt-5.1-codex-mini`` only accepts ``text.verbosity == "medium"``,
      while Codex defaults to ``"low"`` for other models;
    * codex-mini only accepts low/medium/high reasoning effort, so Codex's
      ``xhigh`` has to be clamped.

    ``AGENT_REASONING_EFFORT`` is an explicit operator override (e.g. to cap
    cost) and takes precedence over the clamp.
    """
    import os

    if "codex-mini" in model:
        text_cfg = body.get("text")
        if isinstance(text_cfg, dict) and text_cfg.get("verbosity") not in (
            None,
            "medium",
        ):
            logging.info(
                f"[Agent/normalize] coercing text.verbosity="
                f"{text_cfg.get('verbosity')!r} → 'medium' for {model!r}"
            )
            text_cfg["verbosity"] = "medium"

    reasoning_cfg = body.get("reasoning")
    if not isinstance(reasoning_cfg, dict):
        return

    effort_override = os.getenv("AGENT_REASONING_EFFORT", "").strip()
    if effort_override:
        if reasoning_cfg.get("effort") != effort_override:
            logging.info(
                f"[Agent/normalize] reasoning.effort override via env — "
                f"{reasoning_cfg.get('effort')!r} → {effort_override!r}"
            )
            reasoning_cfg["effort"] = effort_override
        return

    if "codex-mini" in model:
        effort = reasoning_cfg.get("effort")
        if isinstance(effort, str) and effort not in _VALID_REASONING_EFFORTS:
            logging.info(
                f"[Agent/normalize] coercing reasoning.effort={effort!r} → 'high' "
                f"for {model!r}"
            )
            reasoning_cfg["effort"] = "high"


# ── Response parsing ─────────────────────────────────────────────────────────

_ParsedResponse = tuple[
    Optional[int], Optional[int], Optional[int], Optional[str], Optional[str]
]


def parse_stream(raw_sse: str) -> _ParsedResponse:
    """Extract (prompt_tokens, completion_tokens, total_tokens, finish_reason,
    response_text) from a Chat Completions SSE stream.

    Token counts only appear when the request set
    ``stream_options.include_usage``; otherwise they come back None.
    """
    content_parts: list[str] = []
    finish_reason = None
    prompt_tokens = completion_tokens = total_tokens = None

    for line in raw_sse.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        # Usage chunk (sent when stream_options.include_usage=true): carries
        # usage with an empty choices array.
        if "usage" in chunk and chunk.get("choices") == []:
            u = chunk["usage"] or {}
            prompt_tokens = u.get("prompt_tokens")
            completion_tokens = u.get("completion_tokens")
            total_tokens = u.get("total_tokens")
            continue

        for choice in chunk.get("choices", []):
            delta_content = choice.get("delta", {}).get("content")
            if delta_content:
                content_parts.append(delta_content)
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    return (
        prompt_tokens,
        completion_tokens,
        total_tokens,
        finish_reason,
        "".join(content_parts) or None,
    )


def parse_responses_api_stream(raw_sse: str) -> _ParsedResponse:
    """Same extraction for a Responses API SSE stream.

    The Responses API uses named events (``response.output_text.delta``,
    ``response.completed``) rather than Chat Completions' ``choices[].delta``
    shape, so it needs its own parser — without this, every streaming Codex
    call records null tokens and no response text.
    """
    content_parts: list[str] = []
    finish_reason = None
    prompt_tokens = completion_tokens = total_tokens = None

    for line in raw_sse.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue

        event_type = chunk.get("type") or ""
        if event_type.endswith("output_text.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str):
                content_parts.append(delta)
        elif event_type in ("response.completed", "response.incomplete"):
            response = chunk.get("response") or {}
            usage = response.get("usage") or {}
            # The Responses API names these input/output rather than
            # prompt/completion.
            prompt_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
            completion_tokens = usage.get(
                "output_tokens", usage.get("completion_tokens")
            )
            total_tokens = usage.get("total_tokens")
            finish_reason = response.get("status") or (
                "completed" if event_type == "response.completed" else "incomplete"
            )

    return (
        prompt_tokens,
        completion_tokens,
        total_tokens,
        finish_reason,
        "".join(content_parts) or None,
    )


def extract_from_response(resp_json: dict) -> _ParsedResponse:
    """Extract the same tuple from a non-streaming Chat Completions response."""
    usage = resp_json.get("usage", {}) or {}
    choice = (resp_json.get("choices") or [{}])[0]
    content = (choice.get("message", {}) or {}).get("content") or ""
    return (
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
        usage.get("total_tokens"),
        choice.get("finish_reason"),
        content or None,
    )


def extract_from_responses_api(resp_json: dict) -> _ParsedResponse:
    """Extract the same tuple from a non-streaming Responses API response."""
    usage = resp_json.get("usage", {}) or {}
    text_parts: list[str] = []
    for item in resp_json.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    return (
        usage.get("input_tokens", usage.get("prompt_tokens")),
        usage.get("output_tokens", usage.get("completion_tokens")),
        usage.get("total_tokens"),
        resp_json.get("status"),
        "".join(text_parts) or None,
    )
