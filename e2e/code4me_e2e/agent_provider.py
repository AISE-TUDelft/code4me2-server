"""Loopback OpenAI transport for native Goose and Codex ACP probes.

The provider implements only the two wire protocols exercised by the native
agents: Chat Completions for Goose and Responses for the vendored Codex ACP
adapter.  It deliberately keeps request receipts to metadata, never request
bodies or authentication headers, so a test can prove a fresh outbound call
without preserving prompt or credential material.
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

__all__ = ["AgentProvider", "NativeAgentProvider"]


#: The research inference gateway's Chat Completions path (no leading slash
#: on the Goose side; the provider serves it with one).
GATEWAY_CHAT_ROUTE = "/api/research/inference/v1/chat/completions"


class AgentProvider:
    """A deterministic, ephemeral-port OpenAI-compatible loopback provider.

    ``chat_routes`` are the Chat Completions paths it answers (the classic
    ``/v1/chat/completions`` and the research gateway path by default).
    ``expected_bearer`` lets a receipt record whether the ``Authorization``
    header matched (equality only; the value is never stored). With
    ``quota_exhausted`` every chat call is refused with the research gateway's
    ``402 quota_exhausted`` body, which is how a used-up participant budget
    looks to the agent.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        chat_routes: Optional[List[str]] = None,
        expected_bearer: Optional[str] = None,
        quota_exhausted: bool = False,
    ) -> None:
        self.token = token or f"E2E_NATIVE_ANSWER_{uuid.uuid4().hex}"
        self.chat_routes = list(chat_routes or ["/v1/chat/completions", GATEWAY_CHAT_ROUTE])
        self.expected_bearer = expected_bearer
        self.quota_exhausted = bool(quota_exhausted)
        self.requests: List[Dict[str, Any]] = []
        self.port: Optional[int] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._request_count = 0

    def start(self, port: int = 0) -> int:
        """Start on ``127.0.0.1`` and return the selected port."""
        if self._server is not None:
            return self.port or 0
        server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(self))
        server.daemon_threads = True
        self._server = server
        self.port = int(server.server_address[1])
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="code4me-e2e-native-provider",
            daemon=True,
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        """Stop the serving thread cleanly; repeated calls are harmless."""
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("AgentProvider has not been started")
        return f"http://127.0.0.1:{self.port}"

    def record(
        self, *, route: str, model: Any, stream: bool, auth_matched: Optional[bool] = None
    ) -> None:
        """Add a metadata-only, monotonic request receipt (never a header value)."""
        with self._lock:
            self._request_count += 1
            receipt = {
                "route": route,
                "model": str(model) if model is not None else None,
                "stream": bool(stream),
                "count": self._request_count,
                # Kept for drop-in compatibility with ``StubProvider`` callers.
                "receipt": self._request_count,
                # None when no expected bearer was configured.
                "auth_matched": auth_matched,
            }
            self.requests.append(receipt)

    def quota_refusal(self) -> Dict[str, Any]:
        """The research gateway's refusal body for a used-up budget."""
        return {
            "error": {
                "message": (
                    "Your study's AI budget is used up (available $0.00; this request "
                    "needs at least $0.01). Ask the study team for a top-up."
                ),
                "type": "insufficient_quota",
                "code": "quota_exhausted",
            }
        }

    def request_count(self) -> int:
        with self._lock:
            return self._request_count

    def requests_since(self, receipt: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self.requests if item["count"] > receipt]

    def chat_completion(self, model: Any) -> Dict[str, Any]:
        return {
            "id": "chatcmpl-e2e-native",
            "object": "chat.completion",
            "created": 0,
            "model": str(model or "e2e-stub-model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": self.token},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def response(self, model: Any) -> Dict[str, Any]:
        message = {
            "id": "msg_e2e_native",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": self.token, "annotations": []}],
        }
        return {
            "id": "resp_e2e_native",
            "object": "response",
            "status": "completed",
            "model": str(model or "e2e-stub-model"),
            "output": [message],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }


# A descriptive alias helps callers distinguish this native-agent fixture from
# the existing backend relay ``StubProvider`` while retaining one implementation.
NativeAgentProvider = AgentProvider


def _make_handler(provider: AgentProvider):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            return

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)

        def _send_sse(self, events: List[tuple[str, Any]]) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for event, payload in events:
                self.wfile.write(f"event: {event}\n".encode("utf-8"))
                if payload == "[DONE]":
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    data = json.dumps(payload, separators=(",", ":"))
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                self.wfile.flush()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path.split("?", 1)[0] == "/health":
                self._send_json(200, {"status": "ok"})
                return
            self._send_json(404, {"error": {"message": "unsupported route", "type": "invalid_request_error"}})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            route = self.path.split("?", 1)[0]
            if route not in (*provider.chat_routes, "/v1/responses"):
                self._send_json(404, {"error": {"message": "unsupported route", "type": "invalid_request_error"}})
                return
            auth_matched: Optional[bool] = None
            if provider.expected_bearer is not None:
                header = self.headers.get("Authorization") or ""
                auth_matched = header == f"Bearer {provider.expected_bearer}"
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length < 0:
                    raise ValueError
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": {"message": "invalid JSON", "type": "invalid_request_error"}})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"error": {"message": "request must be an object", "type": "invalid_request_error"}})
                return

            model, stream = payload.get("model"), bool(payload.get("stream"))
            # Do not retain ``payload``, headers, or any prompt/auth values.
            provider.record(route=route, model=model, stream=stream, auth_matched=auth_matched)
            if route in provider.chat_routes:
                if provider.quota_exhausted:
                    # The research gateway refuses before anything reaches a
                    # provider; 402 is deliberately not a retried status.
                    self._send_json(402, provider.quota_refusal())
                    return
                self._chat(model, stream)
            else:
                self._responses(model, stream)

        def _chat(self, model: Any, stream: bool) -> None:
            completion = provider.chat_completion(model)
            if not stream:
                self._send_json(200, completion)
                return
            chunk = {
                "id": completion["id"], "object": "chat.completion.chunk", "created": 0,
                "model": completion["model"],
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": provider.token}, "finish_reason": None}],
            }
            done = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            self._send_sse([("chat.completion.chunk", chunk), ("chat.completion.chunk", done), ("done", "[DONE]")])

        def _responses(self, model: Any, stream: bool) -> None:
            response = provider.response(model)
            if not stream:
                self._send_json(200, response)
                return
            started = {**response, "status": "in_progress", "output": []}
            in_progress_item = {
                "id": "msg_e2e_native", "type": "message", "status": "in_progress",
                "role": "assistant", "content": [],
            }
            completed_item = response["output"][0]
            self._send_sse([
                ("response.created", {"type": "response.created", "response": started}),
                ("response.in_progress", {"type": "response.in_progress", "response": started}),
                ("response.output_item.added", {
                    "type": "response.output_item.added", "output_index": 0, "item": in_progress_item,
                }),
                ("response.content_part.added", {
                    "type": "response.content_part.added", "item_id": "msg_e2e_native",
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }),
                ("response.output_text.delta", {
                    "type": "response.output_text.delta", "item_id": "msg_e2e_native",
                    "output_index": 0, "content_index": 0, "delta": provider.token,
                }),
                ("response.output_text.done", {
                    "type": "response.output_text.done", "item_id": "msg_e2e_native",
                    "output_index": 0, "content_index": 0, "text": provider.token,
                }),
                ("response.content_part.done", {
                    "type": "response.content_part.done", "item_id": "msg_e2e_native",
                    "output_index": 0, "content_index": 0,
                    "part": completed_item["content"][0],
                }),
                ("response.output_item.done", {
                    "type": "response.output_item.done", "output_index": 0, "item": completed_item,
                }),
                ("response.completed", {"type": "response.completed", "response": response}),
            ])

    return Handler
