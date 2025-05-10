import json
import uuid
from typing import Optional

import redis.exceptions
from redis import Redis


class SessionManager:
    redis_client = None
    SESSION_EXPIRY_SECONDS = 3600  # 1 hour

    def __init__(self, host="localhost", port=6379):
        self.redis_client = Redis(host=host, port=port)
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
            SessionManager.SESSION_EXPIRY_SECONDS,
            session_data,
        )
        return session_token

    def get_session(self, session_token: str) -> Optional[dict]:
        session_data = self.redis_client.get(f"session:{session_token}")
        return json.loads(session_data) if session_data else None

    def delete_session(self, session_token: str) -> None:
        self.redis_client.delete(f"session:{session_token}")

    def delete_user_sessions(self, user_id: uuid.UUID) -> None:
        keys = self.redis_client.keys(f"session:*")
        for key in keys:
            session_data = json.loads(self.redis_client.get(key))
            if session_data and session_data["user_id"] == str(user_id):
                self.redis_client.delete(key)
