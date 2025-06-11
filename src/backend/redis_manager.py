import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import redis.exceptions
from redis import Redis
from sqlalchemy.orm import Session

import database.crud as crud
import Queries
from backend.utils import recursive_json_loads


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
        token_hook_activation_in_seconds: int = 60,
        store_multi_file_context_on_db: bool = True,
    ):
        self.__redis_client = Redis(host=host, port=port, decode_responses=True)
        self.session_token_expires_in_seconds = session_token_expires_in_seconds
        self.auth_token_expires_in_seconds = auth_token_expires_in_seconds
        self.store_multi_file_context_on_db = store_multi_file_context_on_db
        self.token_hook_activation_in_seconds = token_hook_activation_in_seconds
        try:
            self.__redis_client.ping()
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
        elif type == "email_verification":
            return 86400
        else:
            return 3600

    def __get_reset_exp(self, type: str) -> bool:
        return type == "session_token"

    def __get_set_hook(self, type: str) -> bool:
        return type in ["session_token", "auth_token"]

    def set(self, type: str, token: str, info: dict, force_reset_exp: bool = False):
        key = f"{type}:{token}"
        json_info = json.dumps(info)
        if force_reset_exp or self.__get_reset_exp(type):
            expiration = self.__get_exp(type)
            self.__redis_client.setex(key, expiration, json_info)
            if self.__get_set_hook(type):
                # Set the expiration of the hook to (expiration - token_hook_activation_in_seconds) seconds
                self.__redis_client.setex(
                    f"{type}_hook:{token}",
                    expiration - self.token_hook_activation_in_seconds,
                    "",
                )
        else:
            self.__redis_client.set(key, json_info, keepttl=True)

    def get(self, type: str, token: str, reset_exp: bool = False) -> Optional[dict]:
        if not token:
            return None
        data = self.__redis_client.get(f"{type}:{token}")
        if data:
            if reset_exp:
                expiration = self.__get_exp(type)
                self.__redis_client.setex(f"{type}:{token}", expiration, str(data))
                if self.__get_set_hook(type):
                    self.__redis_client.setex(
                        f"{type}_hook:{token}",
                        expiration - self.token_hook_activation_in_seconds,
                        "",
                    )
            return recursive_json_loads(data)  # type: ignore
        return None

    def delete(self, type: str, token: str, db_session: Session):
        key = f"{type}:{token}"
        if type == "auth_token":
            auth_dict = self.get(type, token)
            self.__redis_client.delete(key)
            self.__redis_client.delete(f"{type}_hook:{token}")
            if auth_dict:
                session_token = auth_dict.get("session_token", "")
                if session_token:
                    self.delete(
                        "session_token",
                        session_token,
                        db_session,
                    )
        elif type == "session_token":
            crud.update_session(
                db_session,
                uuid.UUID(token),
                Queries.UpdateSession(end_time=datetime.now().isoformat()),
            )
            session_dict = self.get(type, token)
            self.__redis_client.delete(key)
            self.__redis_client.delete(f"{type}_hook:{token}")
            if session_dict:
                for project_token in session_dict.get("project_tokens", []):
                    project_dict = self.get("project_token", project_token)
                    if project_dict:
                        new_session_tokens = project_dict.get("session_tokens", [])
                        new_session_tokens.remove(token)
                        project_dict["session_tokens"] = new_session_tokens
                        self.set("project_token", project_token, project_dict)
                    self.delete("project_token", project_token, db_session)
                auth_token = session_dict.get("auth_token", "")
                auth_dict = self.get("auth_token", auth_token)
                if auth_token and auth_dict:
                    auth_dict["session_token"] = ""
                    self.set("auth_token", auth_token, auth_dict)
        elif type == "project_token":
            project_dict = self.get(type, token)
            if project_dict:
                if len(project_dict.get("session_tokens", [])) == 0:
                    if self.store_multi_file_context_on_db:
                        multi_file_contexts = project_dict.get(
                            "multi_file_contexts", {}
                        )
                        multi_file_context_changes = project_dict.get(
                            "multi_file_context_changes", {}
                        )

                        # Check if there exists a user of this project which has not allowed us to store context don't store the context
                        project_users = crud.get_project_users(db_session, token)
                        allowed_to_store_context = True
                        for user_project in project_users:
                            user = crud.get_user_by_id(db_session, user_project.user_id)
                            if user and not json.loads(user.preference).get(
                                "store_context", False
                            ):
                                allowed_to_store_context = False
                                break
                        if allowed_to_store_context:
                            crud.update_project(
                                db_session,
                                uuid.UUID(token),
                                Queries.UpdateProject(
                                    multi_file_contexts=multi_file_contexts,
                                    multi_file_context_changes=multi_file_context_changes,
                                ),
                            )
                    self.__redis_client.delete(key)

    def listen_for_expired_keys(self, db_session: Session):
        """
        Listens for expired hooks and persists expired data to DB.
        """
        pubsub = self.__redis_client.pubsub()
        pubsub.psubscribe("__keyevent@0__:expired")
        logging.info("Listening for expired Redis keys...")

        for message in pubsub.listen():
            if message["type"] == "pmessage":
                expired_key = message["data"]
                logging.info(f"Key {expired_key} expired in redis")
                token = expired_key.split(":")[1]
                try:
                    if expired_key.startswith("session_token_hook:"):
                        self.delete("session_token", token, db_session)
                    elif expired_key.startswith("auth_token_hook:"):
                        self.delete("auth_token", token, db_session)
                except Exception as e:
                    logging.error(
                        f"Exception occurred when trying to expire {expired_key} in redis: {e}"
                    )

    def cleanup(self, db_session: Session):
        """
        Clean up the Redis and move information to db (use with caution).
        """
        patterns = ["session_token:*", "project_token:*", "auth_token:*"]
        for pattern in patterns:
            for key in self.__redis_client.keys(pattern):  # type: ignore
                type, token = key.split(":")
                self.delete(type, token, db_session)
        self.__redis_client.flushdb()
        self.__redis_client.close()
