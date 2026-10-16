"""
Switch for the classic model-backed endpoints.

The classic plugin surfaces (inline completion and chat) run the models listed in
``model_name``; a local row is downloaded and loaded on the first request that uses
it. With ``CLASSIC_MODELS_ENABLED=false`` (the no-GPU dev stack) those requests are
refused here, before any route or model code runs, so no model is ever loaded.
Every other endpoint, including completion feedback and chat history, is unaffected.
"""

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from backend.Responses import ClassicModelsDisabled, JsonResponseWithStatus

# The only routes that run a classic model; the WebSocket ones feed the Celery
# ``llm`` queue.
CLASSIC_MODEL_PATHS = frozenset(
    {
        "/api/completion/request",
        "/api/chat/request",
        "/api/ws/completion",
        "/api/ws/chat",
    }
)

# WebSocket close code 1013 ("Try Again Later"): the server is up but does not
# serve this endpoint.
WEBSOCKET_CLOSE_CODE = 1013


class ClassicModelsGate:
    """ASGI middleware refusing the classic model endpoints (HTTP 503, WebSocket close)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or (
            scope["path"].rstrip("/") not in CLASSIC_MODEL_PATHS
        ):
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            response = JsonResponseWithStatus(status_code=503, content=ClassicModelsDisabled())
            await response(scope, receive, send)
        else:
            await WebSocketClose(code=WEBSOCKET_CLOSE_CODE)(scope, receive, send)
