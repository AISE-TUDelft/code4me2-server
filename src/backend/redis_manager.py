import json
import logging
import uuid
from datetime import datetime
from typing import Optional, cast

import redis.exceptions
from redis import Redis
from sqlalchemy.orm import Session

import database.crud as crud
import Queries


class RedisManager:
    """
    RedisManager handles authentication and session state for users using Redis as a fast-access store.
    It manages auth_token -> { user_id, session_token } pairs and ensures session lifecycle via Redis expiration.
    """

    def __init__(
        self,
        host: str,
        port: int,
        auth_token_expires_in_seconds: int = 86400,
        session_token_expires_in_seconds: int = 3600,
        store_multi_file_context_on_db: bool = False,
    ):
        self.redis_client = Redis(host=host, port=port, decode_responses=True)
        self.session_token_expires_in_seconds = session_token_expires_in_seconds
        self.auth_token_expires_in_seconds = auth_token_expires_in_seconds
        self.store_multi_file_context_on_db = store_multi_file_context_on_db

        try:
            self.redis_client.ping()
            logging.info(f"Connected to Redis server at {host}:{port}.")
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Check your configuration."
            )

    def __get_exp(self, type: str) -> int:
        if type == "auth_token":
            return self.auth_token_expires_in_seconds
        elif type == "session_token":
            return self.session_token_expires_in_seconds
        elif type == "project_token":
            return -1
        else:
            return 3600

    def __get_reset_exp(self, type: str) -> bool:
        return type == "session_token"

    def set(self, type: str, token: str, info: dict):
        key = f"{type}:{token}"
        expiration = self.redis_client.ttl(key)
        if (
            self.__get_reset_exp(type)
            or expiration is None
            or not isinstance(expiration, int)
            or expiration <= 0
        ):
            expiration = self.__get_exp(type)
        json_info = json.dumps(info)
        if expiration != -1:
            self.redis_client.setex(key, expiration, json_info)
        else:
            self.redis_client.set(key, json_info)

    def get(self, type: str, token: str, reset_exp: bool = False) -> Optional[dict]:
        if token is None:
            return None
        data = self.redis_client.get(f"{type}:{token}")
        if data:
            if reset_exp:
                self.redis_client.setex(
                    f"{type}:{token}", self.__get_exp(type), str(data)
                )
            return json.loads(str(data))
        return None

    def delete(self, type: str, token: str, db_session: Session):
        key = f"{type}:{token}"
        if type == "auth_token":
            auth_dict = self.get(type, token)
            if auth_dict:
                self.redis_client.delete(key)
                self.delete(
                    "session_token",
                    auth_dict.get("session_token", ""),
                    db_session,
                )
        elif type == "session_token":
            session_dict = self.get(type, token)
            if session_dict:
                self.redis_client.delete(key)
                crud.update_session(
                    db_session,
                    uuid.UUID(token),
                    Queries.UpdateSession(end_time=datetime.now().isoformat()),
                )
                for project_token in session_dict.get("project_tokens", []):
                    project_dict = self.get(type, token)
                    if project_dict:
                        new_session_tokens = project_dict.get("session_tokens", [])
                        new_session_tokens.remove(key)
                        project_dict["session_tokens"] = new_session_tokens
                        self.set("project_token", project_token, project_dict)
                    self.delete("project_token", project_token, db_session)
                auth_token = session_dict.get("auth_token", "")
                auth_dict = self.get("auth_token", auth_token)
                if auth_dict:
                    auth_dict["auth_token"] = ""
                    self.set("auth_token", auth_token, auth_dict)
        elif type == "project_token":
            project_dict = self.get(type, token)
            if project_dict:
                if len(project_dict.get("session_tokens", [])) == 0:
                    self.redis_client.delete(key)
                    if self.store_multi_file_context_on_db:
                        multi_file_contexts = project_dict.get("multi_file_contexts")
                        multi_file_context_changes = project_dict.get(
                            "multi_file_context_changes"
                        )
                        if multi_file_contexts and multi_file_context_changes:
                            crud.update_project(
                                db_session,
                                uuid.UUID(token),
                                Queries.UpdateProject(
                                    multi_file_contexts=json.dumps(multi_file_contexts),
                                    multi_file_context_changes=json.dumps(
                                        multi_file_context_changes
                                    ),
                                ),
                            )

    # def create_or_get_session_token(self, auth_token: str) -> Optional[str]:
    #     """
    #     Returns session_token if it exists for the auth_token, else creates and assigns a new one.
    #     Sets session info with expiration and ensures hook for expiration tracking.
    #     """
    #     user_data = self.get_auth_info(auth_token)
    #     if not user_data:
    #         return None

    #     if user_data.get("session_token"):
    #         return user_data["session_token"]

    #     session_token = str(create_uuid())
    #     session_data = {"project_tokens": []}
    #     self.__add_session_with_hook(session_token, json.dumps(session_data))

    #     # Reassign auth_token with updated session_token but preserve TTL
    #     ttl = self.redis_client.ttl(f"auth_token:{auth_token}")
    #     user_data["session_token"] = session_token
    #     self.redis_client.setex(f"auth_token:{auth_token}", ttl, json.dumps(user_data))

    #     return session_token

    def __add_session_with_hook(self, session_token: str, value: str) -> None:
        """
        Sets session key and its expiration hook.
        """
        self.redis_client.setex(
            f"session:{session_token}", self.session_token_expires_in_seconds, value
        )
        self.redis_client.setex(
            f"session_hook:{session_token}",
            self.session_token_expires_in_seconds - 5,
            "",
        )

    def delete_session(self, session_token: str) -> None:
        """
        Manually delete session and hook.
        """
        self.redis_client.delete(f"session:{session_token}")
        self.redis_client.delete(f"session_hook:{session_token}")

    # #TODO
    # def move_session_info_to_db(self, db: Session, session_token: str):
    #     """
    #     Move session data to persistent storage (PostgreSQL) and delete from Redis.
    #     """
    #     session_info = self.redis_client.get(f"session:{session_token}")
    #     if session_info:
    #         session_info = json.loads(session_info)
    #         crud.delete_session_by_id(db, session_token)
    #         crud.add_session(
    #             db,
    #             db_schemas.Session(
    #                 session_id=session_token,
    #                 user_id=session_info.get("user_id"),
    #                 multi_file_contexts=json.dumps(session_info.get("data", {}).get("context", {})),
    #                 multi_file_context_changes=json.dumps(session_info.get("data", {}).get("context_changes", {})),
    #             ),
    #         )

    # # def create_project_if_not_exists(self,db)
    # #TODO
    # def listen_for_expired_keys(self, db: Session):
    #     """
    #     Listens for expired session hooks and persists expired sessions to DB.
    #     """
    #     pubsub = self.redis_client.pubsub()
    #     pubsub.psubscribe("__keyevent@0__:expired")
    #     logging.info("Listening for expired Redis keys...")

    #     for message in pubsub.listen():
    #         if message["type"] == "pmessage":
    #             expired_key = message["data"]
    #             if expired_key.startswith("session_hook:"):
    #                 session_token = expired_key.split(":")[1]
    #                 lock_key = f"lock:session:{session_token}"
    #                 lock = self.redis_client.lock(lock_key, timeout=10)
    #                 if lock.acquire(blocking=False):
    #                     try:
    #                         logging.info(f"Session expired: {expired_key}. Archiving to DB...")
    #                         self.move_session_info_to_db(db, session_token)
    #                     finally:
    #                         lock.release()

    def cleanup(self, db: Session):
        """
        Move all session info to DB and clear Redis (use with caution).
        """
        for key in cast("list", self.redis_client.keys("session:*")):
            session_token = key.split(":")[1]
            # self.move_session_info_to_db(db, session_token)
        self.redis_client.flushdb()
        self.redis_client.close()
