from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Any, Callable, Protocol
from urllib import request
from uuid import uuid4

if TYPE_CHECKING:
    from pathlib import Path

    from code4me2_agent.config import AgentConfig


SCHEMA_VERSION = "code4me.agent.event.v1"

_CONTENT_PAYLOAD_KEYS = frozenset(
    {
        "arguments",
        "argv",
        "content",
        "edits",
        "entries",
        "error_message",
        "glob",
        "messages",
        "new_text",
        "old_text",
        "pattern",
        "query",
        "raw_arguments",
        "request",
        "response",
        "result",
        "stderr",
        "stdout",
        "text",
    }
)


def _payload_without_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in _CONTENT_PAYLOAD_KEYS
    }

# TODO this file should be working with the database. and may be moved to another folder.
class TelemetrySink(Protocol):
    def append(self, event: dict[str, Any]) -> None:
        ...


class UploadEventsCallable(Protocol):
    def __call__(
        self,
        run: dict[str, Any],
        events: list[dict[str, Any]],
        auth_headers: dict[str, str],
    ) -> None:
        ...


class JsonlTelemetrySink:
    def __init__(self, trace_path: Path) -> None:
        self._trace_path = trace_path

    def append(self, event: dict[str, Any]) -> None:
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        with self._trace_path.open("a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(event, sort_keys=True))
            trace_file.write("\n")


JsonlTraceRecorder = JsonlTelemetrySink


class ServerUploadTelemetrySink:
    def __init__(
        self,
        upload_events: UploadEventsCallable,
        batch_size: int = 50,
        auth_headers: dict[str, str] | None = None,
        auth_headers_provider: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._upload_events = upload_events
        self._batch_size = max(1, int(batch_size))
        self._auth_headers = dict(auth_headers or {})
        self._auth_headers_provider = auth_headers_provider
        self._pending_run_events: dict[str, list[dict[str, Any]]] = {}

    def set_auth_headers(self, auth_headers: dict[str, str]) -> None:
        self._auth_headers = dict(auth_headers)

    def append(self, event: dict[str, Any]) -> None:
        run_id = event["run_id"]
        pending_events = self._pending_run_events.setdefault(run_id, [])
        pending_events.append(event)
        self._flush_ready_runs()

    def _flush_ready_runs(self) -> None:
        for run_id in list(self._pending_run_events.keys()):
            pending_events = self._pending_run_events[run_id]
            if not self._should_flush(pending_events):
                continue

            run_payload = _build_run_payload(pending_events)
            try:
                headers = (
                    self._auth_headers_provider()
                    if callable(self._auth_headers_provider)
                    else dict(self._auth_headers)
                )
                self._upload_events(run_payload, pending_events, dict(headers))
            except Exception:
                continue
            del self._pending_run_events[run_id]

    def _should_flush(self, pending_events: list[dict[str, Any]]) -> bool:
        if len(pending_events) >= self._batch_size:
            return True
        return any(event["event_type"] == "agent.run.completed" for event in pending_events)


def _build_run_payload(events: list[dict[str, Any]]) -> dict[str, Any]:
    first_event = events[0]
    status = "in_progress"
    started_at = None
    completed_at = None

    for event in events:
        if event["event_type"] == "agent.run.started":
            status = str(event.get("payload", {}).get("status", "started"))
            started_at = event["timestamp"]
        elif event["event_type"] == "agent.run.completed":
            status = str(event.get("payload", {}).get("status", "completed"))
            completed_at = event["timestamp"]

    return {
        "run_id": first_event["run_id"],
        "session_id": first_event["session_id"],
        "status": status,
        "source": first_event.get("source", "code4me2_agent"),
        "started_at": started_at,
        "completed_at": completed_at,
    }


def upload_event_batch_http(
    run: dict[str, Any],
    events: list[dict[str, Any]],
    auth_headers: dict[str, str],
    *,
    ingest_url: str,
    timeout_seconds: float,
) -> None:
    payload = {"run": run, "events": events}
    payload_bytes = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(auth_headers)
    http_request = request.Request(
        ingest_url,
        data=payload_bytes,
        headers=headers,
        method="POST",
    )
    with request.urlopen(http_request, timeout=timeout_seconds) as response:
        if response.status >= 400:
            raise RuntimeError(f"Telemetry upload failed with HTTP {response.status}.")


def build_upload_sink(config: AgentConfig) -> ServerUploadTelemetrySink | None:
    if not config.upload.enabled or config.upload.ingest_url is None:
        return None

    def upload_events(
        run: dict[str, Any],
        events: list[dict[str, Any]],
        auth_headers: dict[str, str],
    ) -> None:
        upload_event_batch_http(
            run=run,
            events=events,
            auth_headers=auth_headers,
            ingest_url=config.upload.ingest_url,
            timeout_seconds=config.upload.timeout_seconds,
        )

    return ServerUploadTelemetrySink(
        upload_events=upload_events,
        batch_size=config.upload.batch_size,
        auth_headers=config.upload.auth_headers,
        auth_headers_provider=config.upload.auth_headers_provider,
    )


class AgentTelemetryRecorder:
    def __init__(self, config: AgentConfig, sinks: list[TelemetrySink] | None = None) -> None:
        self._config = config
        # Participant mode must not leave study prompts, tool arguments, or a
        # surprise .code4me directory in the participant's project. The
        # authenticated server sink remains the telemetry system of record;
        # local JSONL is retained only for explicit developer configurations.
        self._jsonl = None if config.managed_mode else JsonlTelemetrySink(config.trace_path)
        if sinks is None:
            upload_sink = build_upload_sink(config)
            self._sinks = [upload_sink] if upload_sink is not None else []
        else:
            self._sinks = sinks
        self._run_sequences: dict[str, int] = {}
        # Read-only tool calls run in parallel: sequence numbers and sink
        # writes must stay ordered and whole.
        self._lock = RLock()

    def apply_config(self, config: AgentConfig) -> None:
        """Refresh policy metadata and rotating bearer headers without dropping events."""
        self._config = config
        for sink in self._sinks:
            set_auth_headers = getattr(sink, "set_auth_headers", None)
            if callable(set_auth_headers):
                set_auth_headers(config.upload.auth_headers)

    def record(
        self,
        *,
        event_type: str,
        run_id: str,
        request_id: str,
        parent_event_id: str | None,
        payload: dict[str, Any],
        message_id: str | None = None,
        metrics: dict[str, Any] | None = None,
        contains_user_prompt: bool = False,
        contains_agent_response: bool = False,
        raw_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            return self._record_locked(
                event_type=event_type,
                run_id=run_id,
                request_id=request_id,
                parent_event_id=parent_event_id,
                payload=payload,
                message_id=message_id,
                metrics=metrics,
                contains_user_prompt=contains_user_prompt,
                contains_agent_response=contains_agent_response,
                raw_payload=raw_payload,
            )

    def _record_locked(
        self,
        *,
        event_type: str,
        run_id: str,
        request_id: str,
        parent_event_id: str | None,
        payload: dict[str, Any],
        message_id: str | None,
        metrics: dict[str, Any] | None,
        contains_user_prompt: bool,
        contains_agent_response: bool,
        raw_payload: dict[str, Any] | None,
    ) -> dict[str, Any]:
        sequence = self._run_sequences.get(run_id, 0) + 1
        self._run_sequences[run_id] = sequence

        observed_tool = str(payload.get("tool_name", "none"))
        stored_payload = (
            payload
            if self._config.store_agent_content
            else _payload_without_content(payload)
        )
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": uuid4().hex,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "sequence": sequence,
            "source": "code4me2_agent",
            "session_id": self._config.session_id,
            "run_id": run_id,
            "request_id": request_id,
            "parent_event_id": parent_event_id,
            "payload": stored_payload,
            "raw_payload": (
                raw_payload
                if self._config.raw_capture_enabled
                and self._config.store_agent_content
                else None
            ),
            "metrics": metrics or {},
            "privacy": {
                "capture_mode": "raw" if self._config.raw_capture_enabled else "redacted",
                "raw_capture_enabled": self._config.raw_capture_enabled,
                "contains_user_prompt": contains_user_prompt,
                "contains_agent_response": contains_agent_response,
                "redaction_applied": not self._config.raw_capture_enabled,
            },
            "observability": {
                "level": "full",
                "adapter": self._config.adapter.name,
                "llm": self._config.adapter.name,
                "tools": observed_tool,
                "missing_internals": [],
            },
        }
        if message_id is not None:
            event["message_id"] = message_id

        if self._jsonl is not None:
            self._jsonl.append(event)
        for sink in self._sinks:
            sink.append(event)
        return event
