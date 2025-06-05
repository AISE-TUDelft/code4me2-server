import json
import logging
import uuid
from typing import Optional

import redis.exceptions
from redis import Redis
from sqlalchemy.orm import Session

import database.crud as crud
from database import db_schemas


class SessionManager:
    def __init__(
        self,
        host: str,
        port: int,
        auth_token_expires_in_seconds: int = 86400,
        session_token_expires_in_seconds: int = 3600,
    ):
        self.redis_client = Redis(host=host, port=port)
        self.session_token_expires_in_seconds = session_token_expires_in_seconds
        self.auth_token_expires_in_seconds = auth_token_expires_in_seconds
        try:
            self.redis_client.ping()
            logging.log(
                logging.INFO,
                f"Connected to Redis server successfully on {host}:{port}.",
            )
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Please check your configuration."
            )

    def __add_session_with_hook(self, session_token: str, value: str) -> None:
        """
        Sets a key in Redis with an expiration hook.
        The hook is used to handle session expiration.
        """
        self.redis_client.setex(
            f"session:{session_token}", self.session_token_expires_in_seconds, value
        )
        # Set a hook for the key expiration
        self.redis_client.setex(
            f"session_hook:{session_token}",
            self.session_token_expires_in_seconds
            - 5,  # 5 seconds less to ensure the key is deleted before the hook
            "",
        )
        logging.log(
            logging.INFO,
            f"Session value: {self.redis_client.get(f'session:{session_token}')} stored.",
        )

    def delete_user_sessions(self, db: Session, user_id: uuid.UUID) -> None:
        keys = self.redis_client.keys("session:*")
        for key in keys:
            session_data = json.loads(self.redis_client.get(key))
            if session_data and session_data.get("user_id") == str(user_id):
                session_token = key.decode("utf-8").split(":")[1]
                self.move_session_info_to_db(db=db, session_token=session_token)
                self.delete_session(session_token=session_token)

    def delete_user_auths(self, user_id: uuid.UUID) -> None:
        keys = self.redis_client.keys("auth_token:*")
        for key in keys:
            user_id_from_token = self.redis_client.get(key)
            if user_id_from_token and user_id_from_token.decode("utf-8") == str(
                user_id
            ):
                auth_token = key.decode("utf-8").split(":")[1]
                self.redis_client.delete(f"auth_token:{auth_token}")

    def move_session_info_to_db(self, db: Session, session_token: str):
        session_info = self.redis_client.get(f"session:{session_token}")
        if session_info:
            session_info = json.loads(session_info)
            crud.delete_session_by_id(db, session_token)
            crud.add_session(
                db,
                db_schemas.Session(
                    session_id=session_token,
                    user_id=session_info.get("user_id"),
                    multi_file_contexts=json.dumps(
                        session_info.get("data", {}).get("context", {})
                    ),
                    multi_file_context_changes=json.dumps(
                        session_info.get("data", {}).get("context_changes", {})
                    ),
                ),
            )

    # def create_session(self, user_id: uuid.UUID) -> str:
    #     session_token = str(uuid.uuid4())
    #     session_data = json.dumps({"user_id": str(user_id), "data": {}})
    #     self.__add_session_with_hook(session_token, session_data)
    #     return session_token

    def get_session(self, session_token: str) -> Optional[dict]:
        session_data = self.redis_client.get(f"session:{session_token}")
        return json.loads(session_data) if session_data else None

    def update_session(self, session_token: str, session_data: dict) -> None:
        session_data_str = json.dumps(session_data)
        self.__add_session_with_hook(session_token, session_data_str)

    def delete_session(self, session_token: str) -> None:
        self.redis_client.delete(f"session:{session_token}")
        self.redis_client.delete(f"session_hook:{session_token}")

    def cleanup(self, db: Session):
        for key in self.redis_client.keys("session:*"):
            self.move_session_info_to_db(db, key.decode("utf-8").split(":")[1])
        self.redis_client.flushdb()  # Clear all keys in the current database
        self.redis_client.close()

    def create_auth_token(self, user_id: uuid.UUID) -> str:
        token = str(uuid.uuid4())
        self.redis_client.setex(
            f"auth_token:{token}", self.auth_token_expires_in_seconds, str(user_id)
        )
        return token

    def get_user_id_by_auth_token(self, token: str) -> Optional[uuid.UUID]:
        user_id = self.redis_client.get(f"auth_token:{token}")
        if user_id:
            user_id = user_id.decode("utf-8")  # Decode bytes to string
            return uuid.UUID(user_id)
        return None

    def listen_for_expired_keys(self, db: Session):
        pubsub = self.redis_client.pubsub()
        pubsub.psubscribe(
            "__keyevent@0__:expired"
        )  # Subscribe to expired events on Redis
        logging.log(logging.INFO, "Listening for expired keys in Redis...")
        for message in pubsub.listen():
            if message["type"] == "pmessage":
                expired_key = message["data"].decode("utf-8")
                if expired_key.startswith("session_hook:"):
                    session_token = expired_key.split(":")[1]
                    lock_key = f"lock:session:{session_token}"
                    lock = self.redis_client.lock(lock_key, timeout=10)
                    if lock.acquire(blocking=False):  # Do not wait if lock is taken
                        try:
                            # logging.info(f"Acquired lock. Called by {threading.currentThread().name}")
                            logging.info(
                                f"Session expired: {expired_key}. Moving session data to database..."
                            )
                            self.move_session_info_to_db(db, session_token)
                        finally:
                            lock.release()
                    # else:
                    # logging.info(f"Lock for session:{session_token} is already held. Skipping DB write.")

                # elif expired_key.startswith("auth_token:"):
                # auth_token = expired_key.split(":")[1]
                # user_id = self.get_user_id_by_auth_token(auth_token)
                # if user_id:
                #     self.delete_user_sessions(user_id)
