"""A tiny OpenAI-compatible stub provider.

The harness points an administrator-managed ``provider_connection`` at this
process so the real inference relay forwards a chat-completions request here.
The stub answers deterministically with the configured token, which lets the
workflow assert an exact answer end to end without any model or API key.

It serves:

* ``POST /v1/chat/completions`` — non-streaming OpenAI completion.
* ``GET  /health``            — liveness.

It ignores the ``Authorization`` header (the backend always sends the resolved
secret; the stub must not depend on it).
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional


class StubProvider:
    """Threaded HTTP server that echoes a deterministic assistant token."""

    def __init__(self, token: Optional[str] = None) -> None:
        self.token = token or f"E2E_STUB_ANSWER_{uuid.uuid4().hex}"
        self.requests: List[Dict[str, Any]] = []
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self, port: int = 0) -> int:
        if self._server is not None:
            return self.port or 0
        handler = _make_handler(self)
        # A resumed provider connection still names this port. Moving silently
        # would direct the backend to another process and invalidate the test.
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.daemon_threads = True
        self._server = server
        self.port = server.server_address[1]
        self._thread = threading.Thread(
            target=server.serve_forever, name="code4me-e2e-stub", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def record(self, entry: Dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(entry)

    def completion_body(self, requested_model: str) -> Dict[str, Any]:
        return {
            "id": "chatcmpl-e2e",
            "object": "chat.completion",
            "created": 0,
            "model": requested_model or "e2e-stub-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": f"Stub answer: {self.token}",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 4,
                "total_tokens": 7,
            },
        }


def _make_handler(stub: StubProvider):
    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # noqa: D401 - silence access log
            return

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path.split("?")[0] == "/health":
                self._send(200, {"status": "ok", "token": stub.token})
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            if self.path.split("?")[0] != "/v1/chat/completions":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {}
            messages = payload.get("messages") or []
            stub.record(
                {
                    "model": payload.get("model"),
                    "message_count": len(messages),
                    "stream": bool(payload.get("stream")),
                }
            )
            self._send(200, stub.completion_body(str(payload.get("model") or "")))

    return _Handler
