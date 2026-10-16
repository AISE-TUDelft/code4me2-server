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
from privacy.collection import lock_context_storage_allowed


class RedisManager:
    """
    RedisManager handles authentication and session state for users using Redis as a fast-access store.
    It manages auth_token -> { user_id, session_token } pairs and ensures session lifecycle via Redis expiration.
    """

    # Token types whose payload names the owning account in ``user_id``.
    USER_SCOPED_TOKEN_TYPES = (
        "auth_token",
        "acp_grant",
        "acp_session",
        "email_verification",
        "password_reset",
    )

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
        password: Optional[str] = None,
    ):
        # Initialize Redis client with given host and port (and AUTH when the
        # deployment sets `requirepass`, see redis.prod.conf).
        self.__redis_client = Redis(
            host=host,
            port=port,
            password=password or None,
            decode_responses=True,
        )
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

    def ping(self) -> bool:
        """Round-trip to Redis; ``False`` when it is unreachable (health checks)."""
        try:
            return bool(self.__redis_client.ping())
        except Exception:  # noqa: BLE001 - a health probe reports, never raises
            return False

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
        elif type == "acp_grant":
            # One-time launch grant handed to a locally-spawned agent process.
            # Short-lived on purpose: it only has to survive the gap between the
            # plugin writing the handoff file and the agent starting up.
            return 300
        elif type == "acp_pending_grant":
            return 300
        elif type == "acp_session":
            # The agent process's working credential, refreshed on every use.
            return 3600
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
            if type == "session_token":
                self.__extend_account_link(token, info)
        else:
            self.__redis_client.set(key, json_info, keepttl=True)

    def touch(self, type: str, token: str) -> bool:
        """Restart a token's expiry (and its hook's) without rewriting its value,
        so a concurrent update is never lost; False when the token is gone."""
        if not token:
            return False
        expiration = self.__get_exp(type)
        # A token whose hook is gone is ending (its expiry is being handled, or
        # was missed): never extend it.
        if self.__get_set_hook(type) and not self.__redis_client.expire(
            f"{type}_hook:{token}", expiration - self.token_hook_activation_in_seconds
        ):
            return False
        return bool(self.__redis_client.expire(f"{type}:{token}", expiration))

    def __extend_account_link(self, session_token: str, session_info) -> None:
        """Keep the account's link to a session alive as long as the session.

        Project and agent calls find the session through that link only, so a
        link expiring first cut off a session still in use.
        """
        user_id = session_info.get("user_token") if isinstance(session_info, dict) else None
        link = self.get("user_token", user_id) if user_id else None
        if link and link.get("session_token") == session_token:
            self.__redis_client.expire(f"user_token:{user_id}", self.__get_exp("user_token"))

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
                if type == "session_token":
                    self.__extend_account_link(token, recursive_json_loads(data))
            return recursive_json_loads(data)  # Parse JSON string to dict
        return None

    def consume(self, type: str, token: str) -> Optional[dict]:
        """Atomically retrieve and invalidate a one-time token.

        Used for ACP launch grants, where a replayable token would let anyone
        who reads the handoff file mint an agent session. GETDEL makes the
        exchange single-use even under concurrent attempts.
        """
        if not token:
            return None
        data = self.__redis_client.getdel(f"{type}:{token}")
        if data:
            return recursive_json_loads(data)
        return None

    def discard(self, type: str, token: str) -> None:
        """Remove an ephemeral token without the DB-persistence side effects of
        ``delete`` (which tears down whole session graphs)."""
        if token:
            self.__redis_client.delete(f"{type}:{token}")

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
                # Remove the account's link to this session, unless the account
                # has moved on to a newer session meanwhile: an old session that
                # expires must not cut off the one the plugin is using now.
                user_token = session_dict.get("user_token")
                user_info = self.get("user_token", user_token) if user_token else None
                if user_info and user_info.get("session_token") == token:
                    self.__redis_client.delete(f"user_token:{user_token}")

                # Remove this session token from related project tokens, and any
                # session already gone: one whose expiry nobody heard (no listener
                # during a restart) must not keep the project open for good, and
                # a second worker handling this same expiry finds it removed.
                for project_token in session_dict.get("project_tokens", []):
                    project_dict = self.get("project_token", project_token)
                    if project_dict:
                        project_dict["session_tokens"] = [
                            other
                            for other in project_dict.get("session_tokens", [])
                            if other != token
                            and self.__redis_client.exists(f"session_token:{other}")
                        ]
                        self.set("project_token", project_token, project_dict)
                    self.delete("project_token", project_token, db_session)

        elif type == "project_token":
            project_dict = self.get(type, token)
            if project_dict:
                # Only proceed if no active session tokens remain for project token
                if len(project_dict.get("session_tokens", [])) == 0:
                    # A context erased during this session is never written back.
                    if self.store_multi_file_context_on_db and not self.__redis_client.exists(
                        f"context_erased:{token}"
                    ):
                        multi_file_contexts = project_dict.get(
                            "multi_file_contexts", {}
                        )
                        multi_file_context_changes = project_dict.get(
                            "multi_file_context_changes", {}
                        )

                        # Verify all users allow storing context (and have not
                        # opted out of data collection) before persisting. The
                        # locked reads keep a concurrent erase from clearing the
                        # project before this write lands.
                        project_users = crud.get_project_users(db_session, token)
                        allowed_to_store_context = all(
                            lock_context_storage_allowed(db_session, user_project.user_id)
                            for user_project in project_users
                        )
                        if allowed_to_store_context:
                            crud.update_project(
                                db_session,
                                uuid.UUID(token),
                                Queries.UpdateProject(
                                    multi_file_contexts=multi_file_contexts,
                                    multi_file_context_changes=multi_file_context_changes,
                                ),
                            )
                    # Delete project token (and any erase marker) from Redis
                    self.__redis_client.delete(key, f"context_erased:{token}")
        else:
            # For other token types, just delete the key
            self.__redis_client.delete(key)

    def mark_context_erased(self, user_id: str) -> None:
        """
        Keep the account's live project context from being flushed to the database.
        An erase clears the stored context, but the live session goes on serving
        from Redis; without this marker that (pre-erase) context would be written
        back when the session ends, even if the account opts back in meanwhile.
        """
        user_info = self.get("user_token", user_id) or {}
        session_info = self.get("session_token", user_info.get("session_token")) or {}
        for project_token in session_info.get("project_tokens", []):
            # A key of its own: the project entry is rewritten on every context
            # update, which would race a read-modify-write marker.
            self.__redis_client.set(f"context_erased:{project_token}", "1")

    def revoke_user_tokens(self, user_id: str) -> None:
        """
        Invalidate every credential and session of an account, persisting nothing.
        Used once the account is deleted: unlike delete(), which flushes session and
        project state to the database, this only removes keys.
        """
        for type in self.USER_SCOPED_TOKEN_TYPES:
            for key in self.__redis_client.scan_iter(match=f"{type}:*"):
                data = self.__redis_client.get(key)
                info = recursive_json_loads(data) if data else None
                if isinstance(info, dict) and str(info.get("user_id")) == user_id:
                    token = key.split(":", 1)[1]
                    self.__redis_client.delete(key, f"{type}_hook:{token}")

        user_info = self.get("user_token", user_id)
        self.__redis_client.delete(f"user_token:{user_id}")
        session_token = (user_info or {}).get("session_token")
        if not session_token:
            return
        session_info = self.get("session_token", session_token) or {}
        self.__redis_client.delete(
            f"session_token:{session_token}", f"session_token_hook:{session_token}"
        )
        # Detach the session from its projects; a project left without sessions
        # is dropped without flushing its context to the database.
        for project_token in session_info.get("project_tokens", []):
            project_info = self.get("project_token", project_token)
            if not project_info:
                continue
            remaining = [
                token
                for token in project_info.get("session_tokens", [])
                if token != session_token
            ]
            if remaining:
                project_info["session_tokens"] = remaining
                self.set("project_token", project_token, project_info)
            else:
                self.__redis_client.delete(
                    f"project_token:{project_token}", f"context_erased:{project_token}"
                )

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
            logging.info(
                f"Redis connection closed, stopping expired keys listener: {e}"
            )
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
