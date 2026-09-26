"""CLASSIC_MODELS_ENABLED=false refuses the classic model endpoints and nothing else."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.classic_models_gate import (
    CLASSIC_MODEL_PATHS,
    WEBSOCKET_CLOSE_CODE,
    ClassicModelsGate,
)
from backend.Responses import ClassicModelsDisabled
from Code4meV2Config import Code4meV2Config


def make_client(gated: bool) -> TestClient:
    """A stand-in app with the classic routes and one unrelated route."""
    app = FastAPI()

    @app.post("/api/completion/request")
    @app.post("/api/chat/request")
    async def classic_request():
        return {"served": True}

    @app.get("/api/chat/get/{page}")
    async def chat_history(page: int):
        return {"page": page}

    @app.websocket("/api/ws/completion")
    @app.websocket("/api/ws/chat")
    async def classic_socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"served": True})
        await websocket.close()

    if gated:
        app.add_middleware(ClassicModelsGate)
    return TestClient(app)


@pytest.mark.parametrize(
    "path", ["/api/completion/request", "/api/chat/request", "/api/chat/request/"]
)
def test_disabled_refuses_classic_http_requests(path):
    response = make_client(gated=True).post(path, json={})

    assert response.status_code == 503
    assert response.json() == {"message": ClassicModelsDisabled().message}


@pytest.mark.parametrize("path", ["/api/ws/completion", "/api/ws/chat"])
def test_disabled_refuses_classic_websockets(path):
    with pytest.raises(WebSocketDisconnect) as closed:
        with make_client(gated=True).websocket_connect(path):
            pass

    assert closed.value.code == WEBSOCKET_CLOSE_CODE


def test_disabled_leaves_other_endpoints_alone():
    response = make_client(gated=True).get("/api/chat/get/2")

    assert response.status_code == 200
    assert response.json() == {"page": 2}


def test_enabled_serves_classic_endpoints():
    client = make_client(gated=False)

    assert client.post("/api/completion/request", json={}).json() == {"served": True}
    with client.websocket_connect("/api/ws/chat") as socket:
        assert socket.receive_json() == {"served": True}


def test_enabled_by_default_for_backwards_compatibility():
    assert Code4meV2Config.model_fields["classic_models_enabled"].default is True


def test_gated_paths_match_the_real_routes():
    # Guards against a route rename silently making the switch ineffective.
    from main import app

    assert CLASSIC_MODEL_PATHS <= {route.path for route in app.routes}
