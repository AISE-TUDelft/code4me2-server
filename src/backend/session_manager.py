import json
import uuid
from typing import Optional

import redis.exceptions
from redis import Redis


class SessionManager:
    redis_client = None
    session_expires_in_seconds: int = None
    auth_token_expires_in_seconds: int = None

    def __init__(
        self,
        host: str,
        port: int,
        auth_token_expires_in_seconds: int = 86400,
        session_expires_in_seconds: int = 3600,
    ):
        self.redis_client = Redis(host=host, port=port)
        self.session_expires_in_seconds = session_expires_in_seconds
        self.auth_token_expires_in_seconds = auth_token_expires_in_seconds
        try:
            self.redis_client.ping()
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Please check your configuration."
            )

    def create_session(self, user_id: uuid.UUID) -> str:
        session_token = str(uuid.uuid4())
        session_data = json.dumps({"user_id": str(user_id), "data": {}})
        self.redis_client.setex(
            f"session:{session_token}",
            self.session_expires_in_seconds,
            session_data,
        )
        return session_token

    def get_session(self, session_token: str) -> Optional[dict]:
        session_data = self.redis_client.get(f"session:{session_token}")
        return json.loads(session_data) if session_data else None

    def update_session(self, session_token: str, session_data: dict) -> None:
        session_data_str = json.dumps(session_data)
        self.redis_client.setex(
            f"session:{session_token}",
            self.session_expires_in_seconds,
            session_data_str,
        )

    def delete_session(self, session_token: str) -> None:
        self.redis_client.delete(f"session:{session_token}")

    def delete_user_sessions(self, user_id: uuid.UUID) -> None:
        keys = self.redis_client.keys("session:*")
        for key in keys:
            session_data = json.loads(self.redis_client.get(key))
            if session_data and session_data["user_id"] == str(user_id):
                self.redis_client.delete(key)

    def cleanup(self):
        self.redis_client.close()

    def create_auth_token(self, user_id: uuid.UUID) -> str:
        token = str(uuid.uuid4())
        self.redis_client.setex(
            f"auth_token:{token}", self.auth_token_expires_in_seconds, str(user_id)
        )
        return token

    def get_user_id_by_auth_token(self, token: str) -> Optional[uuid.UUID]:
        user_id = self.redis_client.get(f"auth_token:{token}")
        return uuid.UUID(user_id) if user_id else None

    def activate_session(self, session_token: str) -> Optional[dict]:
        session_data = self.get_session(session_token)
        if session_data:
            # Refresh the session expiration
            self.redis_client.expire(
                f"session:{session_token}", self.session_expires_in_seconds
            )
            return session_data
        else:
            return None
