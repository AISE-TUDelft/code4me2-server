"""Authed inference relay backing POST /api/agent/inference.

This is the observation point for **third-party** agents (Goose, Codex). We
don't control their reasoning loops, so instead of asking them to report
telemetry, the plugin's local proxy transparently intercepts every inference
call they make and forwards it here (merge decision 2). That gives us:

* per-call telemetry written into the shared ``agent_event`` table, identical in
  shape to what the self-reporting ``code4me2-agent`` runtime produces;
* server-authoritative model / temperature / tool policy — the agent sends
  whatever its own config says, and we override it from the profile snapshotted
  onto the task, so an A/B arm actually takes effect;
* reconstruction of the tool calls the agent ran locally between requests, by
  diffing the conversation against what we saw last time.

The upstream is a *generic* OpenAI-compatible endpoint resolved from the task's
profile (merge decision 6) — Ollama, Groq, OpenRouter and OpenAI all work
through the same path. Codex's proprietary Responses API is the one special
case, handled as a normalization branch rather than the default.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections import Counter
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

import httpx
from fastapi import Response
from fastapi.responses import StreamingResponse

from agents import provider as provider_module
from agents.event_writer import write_model_call_event, write_tool_call_events
from agents.normalize import (
    StreamingAccumulator,
    extract_from_response,
    extract_from_responses_api,
    extract_tool_executions,
    first_user_message_text,
    is_meta_request,
    normalize_responses_api_body,
    reconcile_openai_passthrough,
    sanitize_schema,
    snippet,
    strip_info_msg,
)
from agents.telemetry import InferenceRecord
from agents.tools import KNOWN_AGENT_TOOLS
from database import crud

if TYPE_CHECKING:
    from datetime import datetime

    from App import App

# Upstream request timeout. Agent turns with large contexts are slow, and a
# premature timeout looks to the developer like the agent hung.
_UPSTREAM_TIMEOUT_SECONDS = 120

# Pre-stream retry budget: retries happen only before the first byte is
# yielded. Mid-stream errors are never retried (no partial-stream replay).
_PRE_STREAM_MAX_ATTEMPTS = 4  # initial + up to 3 retries
_PRE_STREAM_RETRY_STATUSES = frozenset({429, 503})


def _resolve_api_kind(openai_body: dict, api_kind: str = "auto") -> tuple[bool, str]:
    """Resolve the wire API, returning (is_responses_api, effective_kind)."""
    normalized = (api_kind or "auto").strip().lower()
    if normalized in ("chat_completions", "chat", "chat-completions"):
        return False, "chat_completions"
    if normalized in ("responses", "response", "responses_api"):
        return True, "responses"
    # "auto": historical heuristic — Responses API sends `input`.
    inferred = "input" in openai_body
    logging.debug(
        "[Agent/inference] api_kind=auto inferred wire_api=%s",
        "responses" if inferred else "chat_completions",
    )
    return inferred, "auto"


def _retry_after_seconds(headers: httpx.Headers, body: bytes) -> Optional[int]:
    """Extract a provider rate-limit delay so agents can back off correctly."""
    header_value = headers.get("retry-after")
    try:
        if header_value:
            return max(1, min(120, math.ceil(float(header_value))))
    except ValueError:
        pass

    body_text = body.decode("utf-8", errors="replace")
    match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", body_text, re.IGNORECASE)
    if match:
        return max(1, min(120, math.ceil(float(match.group(1)))))
    return None


async def run_inference(
    *,
    task_uuid: uuid.UUID,
    session_uuid: uuid.UUID,
    openai_body: dict,
    enrichment: Optional[dict],
    agent_profile: Optional[str],
    model: str,
    temperature: Optional[float] = None,
    base_url: Optional[str] = None,
    api_key_ref: Optional[str] = None,
    framework_version: Optional[str] = None,
    content_included: bool = False,
    profile_tools_json: Optional[str] = None,
    record_observation_events: bool = True,
    app: App,
    api_kind: str = "auto",
    is_disconnected: Optional[Callable[[], Any]] = None,
    request: Any = None,
) -> Response:
    """Forward one agent inference call upstream and record it as an agent_event.

    The task and session are pre-validated by the route handler, so this
    function trusts its inputs — except ``content_included``, which the route
    resolves from the user's stored preference and which is the only thing
    gating whether message text is persisted.

    ``enrichment`` carries plugin-supplied IDE context (active file, observed
    framework version).
    """
    request_id = str(uuid.uuid4())
    enrichment = enrichment or {}

    # Which wire API this request uses is explicit when the router knows it
    # (managed route passes chat_completions); "auto" falls back to the
    # historical body heuristic (`input` ⇒ Responses API).
    is_responses_api, effective_api_kind = _resolve_api_kind(openai_body, api_kind)

    upstream = provider_module.resolve_upstream(
        model=model,
        base_url=base_url,
        api_key_ref=api_key_ref,
        framework_version=framework_version,
    )

    # Codex → OpenAI is a passthrough: the Responses API is OpenAI's own, so
    # when that's where we're sending it, none of the compat rewrites apply and
    # the payload goes forward essentially untouched. Against any other
    # provider the proprietary fields have to be normalized away.
    is_openai_upstream = "api.openai.com" in upstream.base_url
    use_openai_passthrough = is_responses_api and is_openai_upstream

    requested_model = openai_body.get("model", "unknown")

    if is_responses_api and not use_openai_passthrough:
        logging.info(
            "[Agent/inference] Responses API → non-OpenAI upstream, normalizing"
        )
        normalize_responses_api_body(openai_body)

    # Override the model with the task's assigned one. The agent sends whatever
    # its local config holds; the server decides authoritatively, from the
    # profile snapshotted at task-creation time, so the A/B arm is real.
    if model and model != requested_model:
        logging.info(
            f"[Agent/inference] model override — requested={requested_model!r} "
            f"→ {model!r}"
        )
        openai_body["model"] = model
    elif not model:
        # Defensive: task row had no model — fall back to the requested one so
        # telemetry still has a concrete value to record.
        model = requested_model

    if use_openai_passthrough:
        reconcile_openai_passthrough(openai_body, model)
    elif temperature is not None:
        # Inject the profile's sampling temperature the same way as the model.
        # Skipped on the OpenAI Responses passthrough, which doesn't accept it.
        openai_body["temperature"] = temperature
        logging.info(f"[Agent/inference] temperature override → {temperature}")

    streaming = bool(openai_body.get("stream", False))
    messages = openai_body.get("messages", []) or []
    max_tokens = openai_body.get("max_tokens")
    # The active file path is structural metadata, stored unconditionally.
    active_file: Optional[str] = enrichment.get("active_file")
    observed_framework: Optional[str] = enrichment.get("framework_version")

    current_messages = openai_body.get("input", []) if is_responses_api else messages
    if not isinstance(current_messages, list):
        current_messages = []

    # Count prior assistant turns to derive this call's step index.
    if is_responses_api:
        step_index = sum(
            1
            for item in current_messages
            if isinstance(item, dict) and item.get("role") == "assistant"
        )
    else:
        step_index = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
        )

    context_window_size_bytes = len(
        json.dumps(current_messages, default=str).encode("utf-8")
    )

    # Detect whether this request belongs to a new chat session within the same
    # task, by comparing a fingerprint of the first message against the previous
    # request's. A *changed* first message means a brand-new chat was started;
    # the same first message with a shorter array is more likely context
    # compaction, which is not a new session.
    message_count = len(current_messages)
    first_message_hash: Optional[str] = (
        hashlib.sha256(
            json.dumps(current_messages[0], default=str, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if current_messages
        else None
    )
    chat_session_index = 0
    new_session_detected = False
    prev_message_count = 0
    prev_call_at: Optional[datetime] = None
    # span_id of the previous model_call — the parent of any tool calls the
    # agent has executed since, because it was that response which requested them.
    prev_model_call_span_id: Optional[str] = None
    approval_policy: Optional[str] = None
    meta_request = is_meta_request(current_messages)

    db_lookup = app.get_db_session()
    try:
        last_event = crud.get_last_agent_event_for_session(
            db_lookup, session_uuid, event_type="model_call"
        )
        if last_event:
            prev_call_at = last_event.created_at
            prev_model_call_span_id = last_event.span_id
            prev_first_hash = last_event.first_message_hash
            chat_session_index = last_event.chat_session_index or 0
            if meta_request:
                # Auxiliary request (tool-call summary / chat-title generation):
                # its message array is unrelated to the main chat, so pass the
                # previous session-tracking state through unchanged rather than
                # treating this as a new session — or letting it mask a real one
                # on the next call.
                first_message_hash = prev_first_hash
                message_count = last_event.message_count or message_count
                prev_message_count = message_count
            elif prev_first_hash is not None and prev_first_hash != first_message_hash:
                new_session_detected = True
                chat_session_index += 1
            else:
                prev_message_count = last_event.message_count or 0

        task = crud.get_agent_task(db_lookup, task_uuid)
        if task is not None:
            approval_policy = task.approval_policy
            # framework_version is a per-task property. Backfill the concrete
            # version the proxy observed (e.g. "goose 1.x") over the profile's
            # coarse runtime name, once, rather than on every event.
            if observed_framework and not task.framework_version:
                crud.set_agent_task_framework_version(
                    db_lookup, task_uuid, observed_framework
                )
            task_description = task.task_description
            # task_description is the user's first message — content, so only
            # derive and persist it when the user opted into content storage.
            if task_description is None and content_included:
                first_user_text = first_user_message_text(
                    current_messages, is_responses_api=is_responses_api
                )
                if first_user_text:
                    crud.set_agent_task_description(
                        db_lookup, task_uuid, snippet(first_user_text)
                    )
    except Exception as e:
        logging.warning(
            f"[Agent/inference] session-state lookup failed for task={task_uuid} — {e}"
        )
    finally:
        db_lookup.close()

    # Tool calls the agent executed locally appear, fully resolved, in the
    # messages appended since our last call. Emit one tool_call event per
    # call/result pair so we don't need a reporting hook inside the agent.
    if 0 <= prev_message_count <= len(current_messages):
        new_messages = current_messages[prev_message_count:]
    else:
        new_messages = current_messages
    tool_executions = extract_tool_executions(
        new_messages, is_responses_api=is_responses_api
    )
    if record_observation_events:
        write_tool_call_events(
            app,
            task_uuid,
            chat_session_index,
            tool_executions,
            prev_call_at,
            content_included,
            parent_span_id=prev_model_call_span_id,
        )

    system_content = next(
        (
            m.get("content", "")
            for m in messages
            if isinstance(m, dict) and m.get("role") == "system"
        ),
        "",
    )

    original_tools = openai_body.get("tools", []) or []
    # Whether this condition has any tools available at all — distinguishes a
    # deliberately tool-free arm from one with a non-empty allowlist.
    experiment_tool_access_enabled = bool(original_tools)
    tool_names_requested = [
        t.get("function", {}).get("name")
        for t in original_tools
        if isinstance(t, dict) and t.get("function", {}).get("name")
    ]
    tools_kept = 0
    tools_stripped = 0

    if original_tools and not is_responses_api:
        # Tool filtering and schema sanitisation apply only to Chat Completions.
        # Codex manages its own tool schemas for the Responses API — those are
        # passed through as-is (already normalized above where needed).
        #
        # Distinguish "profile explicitly selected no tools" (tools_json="[]" →
        # empty set, must yield zero tools) from "profile has no tool selection
        # at all" (null/unset → None, falls back to the global catalogue). This
        # distinction is what makes a no-tools A/B arm possible.
        profile_tools: Optional[set[str]] = None
        if profile_tools_json is not None:
            try:
                parsed = json.loads(profile_tools_json)
                if isinstance(parsed, list):
                    profile_tools = {str(t) for t in parsed}
            except (json.JSONDecodeError, TypeError):
                profile_tools = None
        effective_allowlist = (
            profile_tools if profile_tools is not None else KNOWN_AGENT_TOOLS
        )
        kept = []
        for tool in original_tools:
            tool_name = tool.get("function", {}).get("name")
            if tool_name in effective_allowlist or (
                "mcp__*" in effective_allowlist
                and isinstance(tool_name, str)
                and tool_name.startswith("mcp__")
            ):
                kept.append(tool)
        tools_stripped = len(original_tools) - len(kept)
        tools_kept = len(kept)
        for tool in kept:
            sanitize_schema(tool.get("function", {}).get("parameters", {}))
        if kept:
            openai_body["tools"] = kept
        else:
            openai_body.pop("tools", None)
            openai_body.pop("tool_choice", None)
        logging.info(
            f"[Agent/inference] tools — allowlist="
            f"{'profile' if profile_tools is not None else 'catalogue'} "
            f"requested={tool_names_requested} kept={tools_kept} "
            f"stripped={tools_stripped}"
        )

    # stream_options.include_usage is a Chat Completions extension and the only
    # way to get token counts out of a stream; the Responses API reports usage
    # in its completion event instead.
    if streaming and not is_responses_api:
        openai_body.setdefault("stream_options", {})["include_usage"] = True

    # Reasoning models add this to assistant messages, but non-reasoning models
    # reject it with a 400 invalid_request_error. Since the server may override
    # the model to a non-reasoning one, strip it defensively.
    if "messages" in openai_body:
        for msg in openai_body["messages"]:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                msg.pop("reasoning_content", None)

    # Schema size is structural telemetry. Token count is estimated in the
    # analytics layer because providers use different tokenizers.
    tool_schema_bytes = len(
        json.dumps(openai_body.get("tools", []), default=str).encode("utf-8")
    )
    body_bytes = json.dumps(openai_body).encode()
    role_counts = Counter(
        m.get("role", "unknown") for m in messages if isinstance(m, dict)
    )
    last_user = next(
        (
            m.get("content", "")
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ),
        None,
    )
    last_user = (
        strip_info_msg(last_user) or None if isinstance(last_user, str) else None
    )

    # Conversation content is only persisted when the user opted in; otherwise
    # these stay null and only the structural columns are written.
    stored_system_message = (system_content or None) if content_included else None
    stored_last_user = last_user if content_included else None

    span = {
        "span_id": request_id,
        "parent_span_id": str(task_uuid),
        "step_index": step_index,
        "context_window_size_bytes": context_window_size_bytes,
        "first_message_hash": first_message_hash,
        "chat_session_index": chat_session_index,
        "chat_new_session_detected": new_session_detected,
        "experiment_tool_access_enabled": experiment_tool_access_enabled,
        "experiment_approval_policy": approval_policy,
    }
    # Non-content structural extras with no typed column of their own.
    extra = {
        "wire_api": "responses" if is_responses_api else "chat_completions",
        "upstream_base_url": upstream.base_url,
        "openai_passthrough": use_openai_passthrough,
        "meta_request": meta_request,
        "requested_model": requested_model,
        "tool_schema_bytes": tool_schema_bytes,
    }

    upstream_url = upstream.endpoint(responses_api=is_responses_api)
    upstream_headers = {
        "Authorization": f"Bearer {upstream.api_key}",
        "Content-Type": "application/json",
    }

    logging.info(
        f"[Agent/inference] start request_id={request_id} task={task_uuid} "
        f"model={model} stream={streaming} messages={len(messages)} "
        f"max_tokens={max_tokens} → {upstream_url} body={len(body_bytes)}B"
    )

    t0 = time.monotonic()

    def _build_record(
        prompt_tok: Optional[int],
        completion_tok: Optional[int],
        total_tok: Optional[int],
        finish_reason: Optional[str],
        response_text: Optional[str],
        latency_ms: int,
        upstream_status: int,
    ) -> InferenceRecord:
        return InferenceRecord(
            request_id=request_id,
            session_id=str(session_uuid),
            task_id=str(task_uuid),
            agent_profile=agent_profile,
            model=model,
            streaming=streaming,
            message_count=len(messages),
            role_breakdown=dict(role_counts),
            first_system_message=stored_system_message,
            last_user_message=stored_last_user,
            tools_kept=tools_kept,
            tools_stripped=tools_stripped,
            tool_names_requested=tool_names_requested,
            max_tokens=max_tokens,
            active_file=active_file,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            total_tokens=total_tok,
            finish_reason=finish_reason,
            response_text=response_text if content_included else None,
            latency_ms=latency_ms,
            upstream_status=upstream_status,
        )

    if streaming:
        # Pre-stream retry only: 429/503 before the first yielded byte honor
        # Retry-After (max ~3 retries). Mid-stream errors are never retried.
        attempt_count = 0
        total_retry_ms = 0
        stream_client: Optional[httpx.AsyncClient] = None
        upstream_stream: Optional[httpx.Response] = None
        last_error_body = b""
        last_error_status = 502
        last_error_headers: httpx.Headers = httpx.Headers()
        last_error_content_type = "application/json"

        async def _is_client_disconnected() -> bool:
            try:
                if is_disconnected is not None:
                    result = is_disconnected()
                    if isinstance(result, Awaitable):
                        result = await result
                    return bool(result)
                if request is not None:
                    maybe = getattr(request, "is_disconnected", None)
                    if callable(maybe):
                        result = maybe()
                        if isinstance(result, Awaitable):
                            result = await result
                        return bool(result)
            except Exception:
                return False
            return False

        for attempt in range(1, _PRE_STREAM_MAX_ATTEMPTS + 1):
            attempt_count = attempt
            candidate_client = httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_SECONDS)
            candidate_request = candidate_client.build_request(
                "POST",
                upstream_url,
                content=body_bytes,
                headers=upstream_headers,
            )
            try:
                candidate_stream = await candidate_client.send(candidate_request, stream=True)
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                try:
                    await candidate_client.aclose()
                except Exception:
                    pass
                latency_ms = int((time.monotonic() - t0) * 1000)
                transport_status = 503 if isinstance(exc, httpx.TimeoutException) else 502
                record = _build_record(None, None, None, None, None, latency_ms, transport_status)
                _log_record(record)
                extra_err = dict(extra)
                extra_err.update(
                    {
                        "attempt_count": attempt_count,
                        "total_retry_ms": total_retry_ms,
                        "usage_source": "missing",
                    }
                )
                if record_observation_events:
                    write_model_call_event(app, task_uuid, record, latency_ms, span, extra_err)
                logging.warning(
                    f"[Agent/inference] pre-stream transport error "
                    f"{type(exc).__name__} request_id={request_id} — {exc}"
                )
                return Response(
                    content=json.dumps({"error": "upstream unavailable"}).encode("utf-8"),
                    status_code=transport_status,
                    media_type="application/json",
                )
            if candidate_stream.status_code in _PRE_STREAM_RETRY_STATUSES and attempt < _PRE_STREAM_MAX_ATTEMPTS:
                error_body = await candidate_stream.aread()
                retry_after = _retry_after_seconds(candidate_stream.headers, error_body)
                await candidate_stream.aclose()
                await candidate_client.aclose()
                sleep_seconds = float(retry_after) if retry_after is not None else 1.0
                total_retry_ms += int(sleep_seconds * 1000)
                logging.info(
                    f"[Agent/inference] pre-stream retry attempt={attempt} "
                    f"status={candidate_stream.status_code} "
                    f"retry_after={retry_after} request_id={request_id}"
                )
                last_error_body = error_body
                last_error_status = candidate_stream.status_code
                last_error_headers = candidate_stream.headers
                # Sliced sleep: abort the retry budget promptly if the client
                # is already gone instead of sleeping blind.
                _retry_cancelled = False
                _slept = 0.0
                while _slept < sleep_seconds:
                    await asyncio.sleep(min(0.2, sleep_seconds - _slept))
                    _slept += 0.2
                    if await _is_client_disconnected():
                        _retry_cancelled = True
                        break
                if _retry_cancelled:
                    logging.info(
                        f"[Agent/inference] pre-stream retry aborted "
                        f"request_id={request_id} — client disconnected"
                    )
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    record = _build_record(
                        None, None, None, "cancelled_client", None,
                        latency_ms, last_error_status,
                    )
                    _log_record(record)
                    extra_cancel = dict(extra)
                    extra_cancel.update(
                        {
                            "attempt_count": attempt_count,
                            "total_retry_ms": total_retry_ms,
                            "usage_source": "missing",
                            "cancel_source": "client",
                        }
                    )
                    if record_observation_events:
                        write_model_call_event(
                            app, task_uuid, record, latency_ms, span, extra_cancel
                        )
                    return Response(
                        content=last_error_body,
                        status_code=last_error_status,
                        media_type=last_error_content_type,
                    )
                continue
            if candidate_stream.status_code >= 400:
                last_error_body = await candidate_stream.aread()
                last_error_status = candidate_stream.status_code
                last_error_headers = candidate_stream.headers
                last_error_content_type = candidate_stream.headers.get(
                    "content-type", "application/json"
                )
                retry_after = _retry_after_seconds(candidate_stream.headers, last_error_body)
                await candidate_stream.aclose()
                await candidate_client.aclose()
                latency_ms = int((time.monotonic() - t0) * 1000)
                record = _build_record(None, None, None, None, None, latency_ms, last_error_status)
                _log_record(record)
                extra_err = dict(extra)
                extra_err.update(
                    {
                        "attempt_count": attempt_count,
                        "total_retry_ms": total_retry_ms,
                        "usage_source": "missing",
                    }
                )
                if record_observation_events:
                    write_model_call_event(app, task_uuid, record, latency_ms, span, extra_err)
                response_headers = {}
                if retry_after is not None:
                    response_headers["Retry-After"] = str(retry_after)
                return Response(
                    content=last_error_body,
                    status_code=last_error_status,
                    headers=response_headers,
                    media_type=last_error_content_type,
                )
            stream_client = candidate_client
            upstream_stream = candidate_stream
            break

        assert stream_client is not None and upstream_stream is not None
        upstream_status_code = upstream_stream.status_code

        async def _stream():
            accumulator = StreamingAccumulator(
                api_kind="responses" if is_responses_api else "chat_completions"
            )
            bytes_forwarded = 0
            chunks = 0
            t_first_byte_ms: Optional[int] = None
            t_first_visible_ms: Optional[int] = None
            stream_aborted = False
            client_cancelled = False
            try:
                logging.info(
                    f"[Agent/inference] ← upstream "
                    f"status={upstream_status_code} (stream)"
                )
                async for chunk in upstream_stream.aiter_bytes():  # type: ignore[union-attr]
                    if await _is_client_disconnected():
                        client_cancelled = True
                        logging.info(
                            f"[Agent/inference] client disconnected mid-stream "
                            f"request_id={request_id} — closing upstream"
                        )
                        try:
                            await upstream_stream.aclose()  # type: ignore[union-attr]
                        except Exception:
                            pass
                        break
                    if chunk:
                        if t_first_byte_ms is None:
                            t_first_byte_ms = int((time.monotonic() - t0) * 1000)
                        bytes_forwarded += len(chunk)
                        chunks += 1
                        # Incremental parse for timing only; authoritative
                        # usage comes from finalize(). Verbatim passthrough —
                        # never synthesize completion chunks.
                        events = accumulator.feed(chunk)
                        if t_first_visible_ms is None and any(
                            e.get("type") == "text_delta" for e in events
                        ):
                            t_first_visible_ms = int((time.monotonic() - t0) * 1000)
                        yield chunk
                    # Cooperative cancel poll per chunk (cheap).
                    if await _is_client_disconnected():
                        client_cancelled = True
                        try:
                            await upstream_stream.aclose()  # type: ignore[union-attr]
                        except Exception:
                            pass
                        break
            except Exception as e:
                stream_aborted = True
                logging.warning(f"[Agent/inference] stream aborted mid-flight — {e}")
            finally:
                try:
                    await upstream_stream.aclose()  # type: ignore[union-attr]
                except Exception:
                    pass
                try:
                    await stream_client.aclose()  # type: ignore[union-attr]
                except Exception:
                    pass
                # Telemetry is written in `finally` so an aborted/cancelled
                # stream still produces exactly one row.
                latency_ms = int((time.monotonic() - t0) * 1000)
                (
                    prompt_tok,
                    completion_tok,
                    total_tok,
                    finish_reason,
                    response_text,
                    tool_calls_seen,
                    usage_source,
                ) = accumulator.finalize()
                cancel_source: Optional[str] = None
                # A late disconnect must not overwrite a terminal finish the
                # upstream already produced — only claim cancelled_client when
                # the stream never reached one.
                if (client_cancelled or await _is_client_disconnected()) and finish_reason is None:
                    finish_reason = "cancelled_client"
                    cancel_source = "client"
                elif stream_aborted and finish_reason is None:
                    finish_reason = "stream_aborted"

                record = _build_record(
                    prompt_tok,
                    completion_tok,
                    total_tok,
                    finish_reason,
                    response_text,
                    latency_ms,
                    upstream_status_code,
                )
                _log_record(record)
                if record_observation_events:
                    stream_extra = dict(extra)
                    stream_extra.update(
                        {
                            "ttft_ms": t_first_byte_ms,
                            "ttfv_ms": t_first_visible_ms,
                            "bytes_forwarded": bytes_forwarded,
                            "chunks": chunks,
                            "attempt_count": attempt_count,
                            "total_retry_ms": total_retry_ms,
                            "usage_source": usage_source,
                            "cancel_source": cancel_source,
                            "tool_calls_seen": tool_calls_seen,
                        }
                    )
                    stream_extra.update(accumulator.extra_details())
                    write_model_call_event(
                        app, task_uuid, record, latency_ms, span, stream_extra
                    )

        return StreamingResponse(_stream(), media_type="text/event-stream")

    try:
        async with httpx.AsyncClient(timeout=_UPSTREAM_TIMEOUT_SECONDS) as client:
            upstream_resp = await client.post(
                upstream_url, content=body_bytes, headers=upstream_headers
            )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        transport_status = 503 if isinstance(exc, httpx.TimeoutException) else 502
        record = _build_record(None, None, None, None, None, latency_ms, transport_status)
        _log_record(record)
        if record_observation_events:
            extra_err = dict(extra)
            extra_err["usage_source"] = "missing"
            write_model_call_event(app, task_uuid, record, latency_ms, span, extra_err)
        logging.warning(
            f"[Agent/inference] non-stream transport error "
            f"{type(exc).__name__} request_id={request_id} — {exc}"
        )
        return Response(
            content=json.dumps({"error": "upstream unavailable"}).encode("utf-8"),
            status_code=transport_status,
            media_type="application/json",
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    if upstream_resp.status_code >= 400:
        logging.error(
            f"[Agent/inference] upstream error {upstream_resp.status_code}: "
            f"{upstream_resp.text}"
        )

    prompt_tok = completion_tok = total_tok = finish_reason = response_text = None
    try:
        extractor = (
            extract_from_responses_api if is_responses_api else extract_from_response
        )
        prompt_tok, completion_tok, total_tok, finish_reason, response_text = extractor(
            upstream_resp.json()
        )
    except Exception as e:
        # A body we can't parse costs telemetry detail, not the response — the
        # agent still gets whatever the upstream said.
        logging.warning(f"[Agent/inference] could not parse upstream response — {e}")

    record = _build_record(
        prompt_tok,
        completion_tok,
        total_tok,
        finish_reason,
        response_text,
        latency_ms,
        upstream_resp.status_code,
    )
    _log_record(record)
    if record_observation_events:
        write_model_call_event(app, task_uuid, record, latency_ms, span, extra)

    response_headers = {}
    if upstream_resp.status_code == 429:
        retry_after = _retry_after_seconds(upstream_resp.headers, upstream_resp.content)
        if retry_after is not None:
            response_headers["Retry-After"] = str(retry_after)

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
        media_type="application/json",
    )


def _log_record(record: InferenceRecord) -> None:
    status_flag = (
        "OK" if (record.upstream_status or 0) < 400 else f"ERR{record.upstream_status}"
    )
    logging.info(
        f"[Agent/inference] done request_id={record.request_id} "
        f"task_id={record.task_id!r} {status_flag} model={record.model} "
        f"stream={record.streaming} "
        f"tokens(in/out/tot)={record.prompt_tokens}/{record.completion_tokens}/"
        f"{record.total_tokens} finish={record.finish_reason} "
        f"latency_ms={record.latency_ms} "
        f"tools(kept/stripped)={record.tools_kept}/{record.tools_stripped}"
    )
