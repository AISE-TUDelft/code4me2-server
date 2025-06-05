from typing import Dict

from fastapi import WebSocket


class WebSocketManager:
    def __init__(self):
        self.__active_connections: Dict[str, WebSocket] = {}
        self.__token_map: Dict[str, Dict[str, WebSocket]] = (
            {}
        )  # token -> connection_id mapping

    def register(self, connection_id: str, token: str, websocket: WebSocket):
        self.__active_connections[connection_id] = websocket
        self.__token_map[token][connection_id] = websocket

    def unregister(self, connection_id: str):
        self.__active_connections.pop(connection_id, None)

    def get_websockets_by_token(self, token: str) -> Dict[str, WebSocket]:
        return self.__token_map.get(token)
