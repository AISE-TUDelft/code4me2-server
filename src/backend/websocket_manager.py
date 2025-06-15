from typing import Dict

from fastapi import WebSocket


class WebSocketManager:
    """
    Manages active WebSocket connections, grouped by authentication token.
    """

    def __init__(self):
        # Stores all active connections by connection ID
        self.__active_connections: Dict[str, WebSocket] = {}

        # Maps a token to its active WebSocket connections (connection_id -> WebSocket)
        self.__token_map: Dict[str, Dict[str, WebSocket]] = {}

    def register(self, connection_id: str, token: str, websocket: WebSocket):
        """
        Registers a new WebSocket connection under a given token.
        """
        self.__active_connections[connection_id] = websocket
        if token not in self.__token_map:
            self.__token_map[token] = {}
        self.__token_map[token][connection_id] = websocket

    def unregister(self, connection_id: str):
        """
        Unregisters a WebSocket connection by its connection ID.
        """
        self.__active_connections.pop(connection_id, None)
        for token_connections in self.__token_map.values():
            token_connections.pop(connection_id, None)

    def get_websockets_by_token(self, token: str) -> Dict[str, WebSocket]:
        """
        Retrieves all WebSocket connections associated with a given token.
        """
        return self.__token_map.get(token, {})
