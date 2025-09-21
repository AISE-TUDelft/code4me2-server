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
        email_verification_token_expires_in_seconds: int = 86400,
        reset_password_token_expires_in_seconds: int = 900,
        token_hook_activation_in_seconds: int = 60,
        store_multi_file_context_on_db: bool = True,
    ):
        # Initialize Redis client with given host and port
        self.__redis_client = Redis(host=host, port=port, decode_responses=True)
        self.session_token_expires_in_seconds = session_token_expires_in_seconds
        self.auth_token_expires_in_seconds = auth_token_expires_in_seconds
        self.email_verification_token_expires_in_seconds = (
            email_verification_token_expires_in_seconds
        )
        self.reset_password_token_expires_in_seconds = (
            reset_password_token_expires_in_seconds
        )
        self.store_multi_file_context_on_db = store_multi_file_context_on_db
        self.token_hook_activation_in_seconds = token_hook_activation_in_seconds

        # Test connection to Redis server
        try:
            self.__redis_client.ping()
            logging.info(f"Connected to Redis server at {host}:{port}.")
        except redis.exceptions.ConnectionError:
            raise Exception(
                "Could not connect to Redis server. Check your configuration."
            )

    def __get_exp(self, type: str) -> int:
        """
        Get expiration time in seconds for different token types.
        """
        if type == "user_token":
            return self.session_token_expires_in_seconds
        elif type == "auth_token":
            return self.auth_token_expires_in_seconds
        elif type == "session_token":
            return self.session_token_expires_in_seconds
        elif type == "project_token":
            return -1  # project tokens do not expire by default
        elif type == "email_verification":
            return self.email_verification_token_expires_in_seconds
        elif type == "password_reset":
            return self.reset_password_token_expires_in_seconds
        else:
            return 3600  # default 1 hour expiration

    def __get_reset_exp(self, type: str) -> bool:
        """
        Determine if expiration should be reset upon access for the token type.
        """
        return type in ["session_token", "user_token"]

    def __get_set_hook(self, type: str) -> bool:
        """
        Determine if expiration hooks should be set for this token type.
        """
        return type in ["session_token", "auth_token"]

    def set(self, type: str, token: str, info: dict, force_reset_exp: bool = False):
        """
        Store token information in Redis with optional expiration and hooks.
        """
        key = f"{type}:{token}"
        json_info = json.dumps(info)

        # Set the token with expiration if needed, else just set with existing TTL
        if force_reset_exp or self.__get_reset_exp(type):
            expiration = self.__get_exp(type)
            self.__redis_client.setex(key, expiration, json_info)

            # Set hook key with expiration slightly before the token expiration
            if self.__get_set_hook(type):
                self.__redis_client.setex(
                    f"{type}_hook:{token}",
                    expiration - self.token_hook_activation_in_seconds,
                    "",
                )
        else:
            self.__redis_client.set(key, json_info, keepttl=True)

    def get(self, type: str, token: str, reset_exp: bool = False) -> Optional[dict]:
        """
        Retrieve token info from Redis and optionally reset its expiration.
        """
        if not token:
            return None

        data = self.__redis_client.get(f"{type}:{token}")
        if data:
            if reset_exp:
                expiration = self.__get_exp(type)
                # Reset expiration for token key
                self.__redis_client.setex(f"{type}:{token}", expiration, str(data))

                # Reset expiration for associated hook if applicable
                if self.__get_set_hook(type):
                    self.__redis_client.setex(
                        f"{type}_hook:{token}",
                        expiration - self.token_hook_activation_in_seconds,
                        "",
                    )
            return recursive_json_loads(data)  # Parse JSON string to dict
        return None

    def delete(self, type: str, token: str, db_session: Session):
        """
        Delete token and related data from Redis and persist relevant info to DB.
        Handles cascading deletes for related tokens.
        """

        key = f"{type}:{token}"
        if type == "auth_token":
            # Deleting auth token also deletes associated session token
            auth_dict = self.get(type, token)
            self.__redis_client.delete(key)
            self.__redis_client.delete(f"{type}_hook:{token}")
            if auth_dict:
                user_token = auth_dict.get("user_id")
                if user_token:
                    self.delete("user_token", user_token, db_session)

        elif type == "user_token":
            user_dict = self.get(type, token)
            self.__redis_client.delete(key)
            if user_dict:
                # Delete session token associated with user token
                session_token = user_dict.get("session_token")
                if session_token:
                    self.delete("session_token", session_token, db_session)

        elif type == "session_token":
            # Update session end time in DB on session token deletion
            crud.update_session(
                db_session,
                uuid.UUID(token),
                Queries.UpdateSession(end_time=datetime.now().isoformat()),
            )
            session_dict = self.get(type, token)
            self.__redis_client.delete(key)
            self.__redis_client.delete(f"{type}_hook:{token}")

            if session_dict:
                # Remove user token if exists
                user_token = session_dict.get("user_token")
                if user_token:
                    self.__redis_client.delete(f"user_token:{user_token}")

                # Remove this session token from related project tokens
                for project_token in session_dict.get("project_tokens", []):
                    project_dict = self.get("project_token", project_token)
                    if project_dict:
                        new_session_tokens = project_dict.get("session_tokens", [])
                        new_session_tokens.remove(token)
                        project_dict["session_tokens"] = new_session_tokens
                        self.set("project_token", project_token, project_dict)
                    self.delete("project_token", project_token, db_session)

        elif type == "project_token":
            project_dict = self.get(type, token)
            if project_dict:
                # Only proceed if no active session tokens remain for project token
                if len(project_dict.get("session_tokens", [])) == 0:
                    if self.store_multi_file_context_on_db:
                        multi_file_contexts = project_dict.get(
                            "multi_file_contexts", {}
                        )
                        multi_file_context_changes = project_dict.get(
                            "multi_file_context_changes", {}
                        )

                        # Verify all users allow storing context before persisting
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
                    # Delete project token from Redis
                    self.__redis_client.delete(key)
        else:
            # For other token types, just delete the key
            self.__redis_client.delete(key)

    def listen_for_expired_keys(self, session_factory):
        """
        Listen for Redis key expiration events on token hooks and delete expired tokens accordingly.
        Persist expired session or auth tokens into DB.
        """
        pubsub = self.__redis_client.pubsub()
        pubsub.psubscribe("__keyevent@0__:expired")
        logging.info("Listening for expired Redis keys...")

        try:
            for message in pubsub.listen():
                if message["type"] == "pmessage":
                    expired_key = message["data"]
                    logging.info(f"Key {expired_key} expired in redis")
                    token = expired_key.split(":")[1]
                    try:
                        if expired_key.startswith("session_token_hook:"):
                            with session_factory() as db_session:
                                try:
                                    self.delete("session_token", token, db_session)
                                finally:
                                    db_session.close()
                        elif expired_key.startswith("auth_token_hook:"):
                            with session_factory() as db_session:
                                try:
                                    self.delete("auth_token", token, db_session)
                                finally:
                                    db_session.close()
                    except Exception as e:
                        logging.error(
                            f"Exception occurred when trying to expire {expired_key} in redis: {e}"
                        )
        except (redis.exceptions.ConnectionError, ValueError) as e:
            # Handle connection errors gracefully during shutdown
            logging.info(f"Redis connection closed, stopping expired keys listener: {e}")
        except Exception as e:
            logging.error(f"Unexpected error in expired keys listener: {e}")
        finally:
            # Ensure pubsub connection is properly closed
            try:
                pubsub.close()
            except Exception:
                pass

    def close(self):
        """
        Close the Redis connection gracefully.
        """
        try:
            self.__redis_client.close()
        except Exception:
            pass

    def cleanup(self, db_session: Session):
        """
        Clean all tokens from Redis and persist necessary data to DB.
        WARNING: Use with caution as it flushes the entire Redis DB.
        """
        patterns = [
            "session_token:*",
            "project_token:*",
            "auth_token:*",
            "user_token:*",
        ]
        for pattern in patterns:
            # Iterate over all keys matching pattern and delete each
            for key in self.__redis_client.keys(pattern):  # type: ignore
                type, token = key.split(":")
                self.delete(type, token, db_session)
        self.__redis_client.flushdb()
        self.__redis_client.close()
