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

#: Cap for the retained streaming text tail. Telemetry keeps a bounded tail
#: rather than the full body so a large stream cannot grow memory unbounded.
_STREAM_TEXT_TAIL_CAP = 4096

#: Same 4KB tail semantics for the SSE line fragment and per-tool argument
#: buffers, which would otherwise grow without bound on a hostile stream.
_LINE_LEFTOVER_CAP = 4096
_TOOL_ARGUMENTS_CAP = 4096


def _parse_usage_details(usage: dict) -> tuple[Optional[int], Optional[int]]:
    """Extract (cached_tokens, reasoning_tokens) subsets from a usage dict."""
    cached = None
    reasoning = None
    if not isinstance(usage, dict):
        return cached, reasoning
    for details_key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict) and details.get("cached_tokens") is not None:
            try:
                cached = int(details["cached_tokens"])
            except (TypeError, ValueError):
                pass
    for details_key in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(details_key)
        if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
            try:
                reasoning = int(details["reasoning_tokens"])
            except (TypeError, ValueError):
                pass
    # Some providers nest under a generic "details" key.
    details = usage.get("details")
    if isinstance(details, dict):
        if cached is None and details.get("cached_tokens") is not None:
            try:
                cached = int(details["cached_tokens"])
            except (TypeError, ValueError):
                pass
        if reasoning is None and details.get("reasoning_tokens") is not None:
            try:
                reasoning = int(details["reasoning_tokens"])
            except (TypeError, ValueError):
                pass
    return cached, reasoning


def _parse_chat_usage(usage: object) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if not isinstance(usage, dict):
        return None, None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")


def _parse_responses_usage(
    usage: object,
) -> tuple[Optional[int], Optional[int], Optional[int]]:
    if not isinstance(usage, dict):
        return None, None, None
    prompt = usage.get("input_tokens", usage.get("prompt_tokens"))
    completion = usage.get("output_tokens", usage.get("completion_tokens"))
    total = usage.get("total_tokens")
    return prompt, completion, total


class StreamingAccumulator:
    """Incremental SSE parser with bounded memory.

    ``feed`` accepts arbitrary byte chunks (TCP fragmentation safe via a
    leftover line buffer); ``finalize`` returns the authoritative telemetry
    tuple. No full-body buffer is retained — only a leftover line fragment, a
    capped text tail, and per-tool argument fragments.
    """

    def __init__(self, api_kind: str = "chat_completions") -> None:
        self._api_kind = api_kind if api_kind in ("responses", "chat_completions") else "chat_completions"
        self._line_leftover = ""
        self._text_len = 0
        self._text_tail = ""
        self._finish_reason: Optional[str] = None
        self._prompt_tokens: Optional[int] = None
        self._completion_tokens: Optional[int] = None
        self._total_tokens: Optional[int] = None
        self._usage_seen = False
        self._tool_fragments: dict[object, dict] = {}
        self._cached_tokens: Optional[int] = None
        self._reasoning_tokens: Optional[int] = None
        self._last_event_name = ""
        self._done = False
        # Set when any bounded buffer discards head bytes (keeps 4KB tail).
        self._truncated = False

    # -- properties used by callers/tests -----------------------------------
    @property
    def has_visible_text(self) -> bool:
        return self._text_len > 0

    @property
    def bytes_tail(self) -> str:
        return self._text_tail

    @property
    def cached_tokens(self) -> Optional[int]:
        return self._cached_tokens

    @property
    def reasoning_tokens(self) -> Optional[int]:
        return self._reasoning_tokens

    @property
    def tool_calls_seen(self) -> int:
        return len(self._tool_fragments)

    def feed(self, data: bytes | str) -> list[dict]:
        """Consume one upstream chunk; return lightweight delta events."""
        if isinstance(data, (bytes, bytearray)):
            text = bytes(data).decode("utf-8", errors="replace")
        else:
            text = data
        combined = self._line_leftover + text
        # Normalize newlines; SSE frames end with \n (or \r\n).
        combined = combined.replace("\r\n", "\n").replace("\r", "\n")
        lines = combined.split("\n")
        # Last element may be an incomplete line — keep it buffered (capped).
        self._line_leftover = lines.pop() if lines else ""
        if len(self._line_leftover) > _LINE_LEFTOVER_CAP:
            self._line_leftover = self._line_leftover[-_LINE_LEFTOVER_CAP:]
            self._truncated = True
        events: list[dict] = []
        for line in lines:
            events.extend(self._process_line(line))
        return events

    def finalize(self) -> tuple[
        Optional[int], Optional[int], Optional[int], Optional[str], Optional[str], int, str
    ]:
        """Return (prompt, completion, total, finish, text, tool_calls_seen, usage_source)."""
        # Flush any trailing buffered line (a stream without a final newline).
        if self._line_leftover:
            # Process without requiring a newline terminator.
            self._process_line(self._line_leftover)
            self._line_leftover = ""
        finish = self._finish_reason
        if finish is None and self._tool_fragments:
            # Tool-only stream that never sent an explicit finish reason.
            finish = "tool_calls"
        text = self._text_tail or None
        # Do not invent text for tool-only streams.
        if text is not None and not text:
            text = None
        usage_source = "stream_usage" if self._usage_seen else "missing"
        return (
            self._prompt_tokens,
            self._completion_tokens,
            self._total_tokens,
            finish,
            text,
            len(self._tool_fragments),
            usage_source,
        )

    def extra_details(self) -> dict:
        details: dict = {}
        if self._cached_tokens is not None:
            details["cached_tokens"] = self._cached_tokens
        if self._reasoning_tokens is not None:
            details["reasoning_tokens"] = self._reasoning_tokens
        if self._truncated:
            details["truncated"] = True
        return details

    def _append_tool_arguments(self, slot: dict, fragment: str) -> None:
        """Append tool-argument bytes, keeping a bounded 4KB tail."""
        if not fragment:
            return
        merged = slot.get("arguments", "") + fragment
        if len(merged) > _TOOL_ARGUMENTS_CAP:
            merged = merged[-_TOOL_ARGUMENTS_CAP:]
            self._truncated = True
        slot["arguments"] = merged

    # -- internals ------------------------------------------------------------
    def _append_text(self, fragment: str) -> None:
        if not fragment:
            return
        self._text_len += len(fragment)
        self._text_tail = (self._text_tail + fragment)[-_STREAM_TEXT_TAIL_CAP:]

    def _record_usage(self, usage: object, *, responses_style: bool) -> None:
        if not isinstance(usage, dict):
            return
        if responses_style:
            prompt, completion, total = _parse_responses_usage(usage)
        else:
            prompt, completion, total = _parse_chat_usage(usage)
            # Some chat providers use input/output naming.
            if prompt is None and "input_tokens" in usage:
                prompt = usage.get("input_tokens")
            if completion is None and "output_tokens" in usage:
                completion = usage.get("output_tokens")
        # Preserve zero (meaningful) while leaving missing as None. An empty
        # usage dict carries no token fields and must not mark stream_usage.
        if prompt is not None:
            self._prompt_tokens = prompt
        if completion is not None:
            self._completion_tokens = completion
        if total is not None:
            self._total_tokens = total
        if prompt is None and completion is None and total is None:
            return
        self._usage_seen = True
        cached, reasoning = _parse_usage_details(usage)
        if cached is not None:
            self._cached_tokens = cached
        if reasoning is not None:
            self._reasoning_tokens = reasoning

    def _process_line(self, line: str) -> list[dict]:
        events: list[dict] = []
        stripped = line.strip()
        if not stripped:
            return events
        if stripped.startswith(":"):
            return events  # SSE comment / keep-alive
        if stripped.startswith("event:"):
            self._last_event_name = stripped[6:].strip()
            return events
        data: Optional[str] = None
        if stripped.startswith("data:"):
            data = stripped[5:].strip()
        elif stripped.startswith("{") or stripped.startswith("["):
            # Data-only line without the SSE prefix (tolerated).
            data = stripped
        else:
            return events
        if not data or data == "[DONE]":
            if data == "[DONE]":
                self._done = True
            return events
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return events
        if self._api_kind == "responses":
            events.extend(self._process_responses_chunk(chunk))
        else:
            events.extend(self._process_chat_chunk(chunk))
        return events

    def _process_chat_chunk(self, chunk: object) -> list[dict]:
        events: list[dict] = []
        if not isinstance(chunk, dict):
            return events
        # Usage may be colocated with choices — always parse it.
        if "usage" in chunk and isinstance(chunk.get("usage"), dict):
            self._record_usage(chunk["usage"], responses_style=False)
            events.append({"type": "usage"})
        for choice in chunk.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                # Some providers send message instead of delta in streams.
                message = choice.get("message")
                if isinstance(message, dict):
                    delta = message
                else:
                    delta = {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._append_text(content)
                events.append({"type": "text_delta", "text": content})
            # Reasoning deltas are telemetry, not visible text.
            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning_delta, str) and reasoning_delta:
                try:
                    # Track length only; do not surface as visible text.
                    pass
                except Exception:
                    pass
            raw_tool_calls = delta.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                for tc in raw_tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    index = tc.get("index", 0)
                    slot = self._tool_fragments.setdefault(
                        index, {"id": None, "name": None, "arguments": ""}
                    )
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        args = fn.get("arguments")
                        if isinstance(args, str):
                            self._append_tool_arguments(slot, args)
                    events.append({"type": "tool_delta", "index": index})
            if choice.get("finish_reason"):
                self._finish_reason = choice["finish_reason"]
                events.append({"type": "finish", "finish_reason": self._finish_reason})
        return events

    def _process_responses_chunk(self, chunk: object) -> list[dict]:
        events: list[dict] = []
        if not isinstance(chunk, dict):
            return events
        event_type = str(chunk.get("type") or self._last_event_name or "")
        if event_type.endswith("output_text.delta"):
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                self._append_text(delta)
                events.append({"type": "text_delta", "text": delta})
        elif event_type.endswith("function_call_arguments.delta"):
            delta = chunk.get("delta")
            key = chunk.get("item_id", chunk.get("output_index", 0))
            slot = self._tool_fragments.setdefault(
                key, {"id": chunk.get("item_id"), "name": None, "arguments": ""}
            )
            if isinstance(delta, str):
                self._append_tool_arguments(slot, delta)
            events.append({"type": "tool_delta", "index": key})
        elif event_type in ("response.output_item.added", "response.output_item.done"):
            item = chunk.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                key = item.get("id", chunk.get("output_index", len(self._tool_fragments)))
                slot = self._tool_fragments.setdefault(
                    key, {"id": None, "name": None, "arguments": ""}
                )
                if item.get("call_id"):
                    slot["id"] = item.get("call_id")
                elif item.get("id"):
                    slot["id"] = item.get("id")
                if item.get("name"):
                    slot["name"] = item.get("name")
                if isinstance(item.get("arguments"), str):
                    slot["arguments"] = item["arguments"][-_TOOL_ARGUMENTS_CAP:]
                    if len(item["arguments"]) > _TOOL_ARGUMENTS_CAP:
                        self._truncated = True
                events.append({"type": "tool_delta", "index": key})
        elif event_type in ("response.completed", "response.incomplete"):
            response = chunk.get("response") or {}
            usage = response.get("usage") or {}
            self._record_usage(usage, responses_style=True)
            status = response.get("status")
            self._finish_reason = status or (
                "completed" if event_type == "response.completed" else "incomplete"
            )
            events.append({"type": "finish", "finish_reason": self._finish_reason})
        elif event_type == "response.failed":
            response = chunk.get("response") or {}
            self._record_usage(response.get("usage") or {}, responses_style=True)
            self._finish_reason = response.get("status") or "failed"
            events.append({"type": "finish", "finish_reason": self._finish_reason})
        else:
            # Tolerate usage colocated on unknown event envelopes.
            usage = chunk.get("usage")
            if isinstance(usage, dict):
                self._record_usage(usage, responses_style=True)
                events.append({"type": "usage"})
            response = chunk.get("response")
            if isinstance(response, dict) and isinstance(response.get("usage"), dict):
                self._record_usage(response["usage"], responses_style=True)
                events.append({"type": "usage"})
        return events


def parse_stream(raw_sse: str) -> _ParsedResponse:
    """Extract (prompt_tokens, completion_tokens, total_tokens, finish_reason,
    response_text) from a Chat Completions SSE stream.

    Token counts only appear when the request set
    ``stream_options.include_usage``; otherwise they come back None.
    Thin wrapper over :class:`StreamingAccumulator` for backwards compat.
    """
    acc = StreamingAccumulator(api_kind="chat_completions")
    acc.feed(raw_sse.encode("utf-8", errors="replace"))
    prompt_tok, completion_tok, total_tok, finish_reason, text, _, _ = acc.finalize()
    return (prompt_tok, completion_tok, total_tok, finish_reason, text)


def parse_responses_api_stream(raw_sse: str) -> _ParsedResponse:
    """Same extraction for a Responses API SSE stream.

    The Responses API uses named events (``response.output_text.delta``,
    ``response.completed``) rather than Chat Completions' ``choices[].delta``
    shape, so it needs its own parser — without this, every streaming Codex
    call records null tokens and no response text.
    Thin wrapper over :class:`StreamingAccumulator` for backwards compat.
    """
    acc = StreamingAccumulator(api_kind="responses")
    acc.feed(raw_sse.encode("utf-8", errors="replace"))
    prompt_tok, completion_tok, total_tok, finish_reason, text, _, _ = acc.finalize()
    return (prompt_tok, completion_tok, total_tok, finish_reason, text)


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
