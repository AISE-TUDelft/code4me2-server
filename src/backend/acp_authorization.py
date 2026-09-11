"""Authorization for locally-launched ACP agent processes.

The built-in ``code4me2-agent`` runtime runs as a separate OS process on the
developer's machine, started by the IDE plugin. It therefore has no session
cookie and cannot be authenticated the way the plugin is. This module implements
the handoff that gives it a credential of its own:

1. **grant** — the authenticated plugin asks for a one-time launch grant, bound
   to a specific user, session, project and workspace directory. The plugin
   writes it where the agent process will find it.
2. **exchange** — the agent process trades the grant (single-use) for a longer
   lived ``acp_session`` token. Consuming the grant means a leaked handoff file
   can't be replayed after the agent has started.
3. **validate-or-refresh** — every subsequent agent request revalidates and
   slides the session's expiry, so an idle agent expires but an active one
   doesn't.

Every scope is *derived*, never accepted from the caller. An agent request can
only ever act as the user, project and workspace that were bound into the grant
at step 1, and each revalidation re-checks that the parent plugin login and
session are still alive — so logging out in the IDE immediately invalidates the
agent process too, rather than leaving an orphaned credential valid for an hour.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Optional, Protocol, Union


class TokenStore(Protocol):
    """The subset of ``RedisManager`` this service needs.

    Declared as a Protocol so the authorization logic is testable against an
    in-memory dict without a Redis instance.
    """

    def get(
        self, type: str, token: str, reset_exp: bool = False
    ) -> Optional[dict]: ...

    def set(
        self,
        type: str,
        token: str,
        info: dict,
        force_reset_exp: bool = False,
    ) -> None: ...

    def consume(self, type: str, token: str) -> Optional[dict]: ...

    def discard(self, type: str, token: str) -> None: ...


class AcpAuthorizationDenied(Exception):
    """Raised for every authorization failure in this module.

    Deliberately carries a coarse message: the caller maps it to a flat 401 so
    the response doesn't reveal *which* check failed.
    """


@dataclass(frozen=True)
class PreparedAcpGrant:
    grant: str
    workspace: str
    launch_id: Optional[str] = None
    path_format: Optional[str] = None
    managed_protocol_version: Optional[str] = None
    expires_in_seconds: int = 300


@dataclass(frozen=True)
class AcpSessionAuthorization:
    acp_token: str
    user_id: str
    project_id: str
    workspace: str
    expires_in_seconds: int = 3600


@dataclass(frozen=True)
class AcpServerAuthorization:
    """A session scope plus the resolved project record.

    Needed by endpoints that act on project data (e.g. bridging a model turn
    through the backend's own model registry), where the project's multi-file
    context has to be loaded.
    """

    acp_token: str
    user_id: str
    session_id: str
    project_id: str
    project_info: dict[str, Any]
    workspace: str
    expires_in_seconds: int = 3600


def authorize_acp_bearer(
    token_store: TokenStore,
    authorization: str,
    *,
    server_scope: bool = False,
) -> Optional[Union[AcpSessionAuthorization, AcpServerAuthorization]]:
    """Resolve an ``Authorization: Bearer <acp_token>`` header to a scope.

    Returns None on any failure so route handlers can respond with a single
    uniform 401 rather than branching on the reason.
    """
    prefix = "Bearer "
    if not authorization.startswith(prefix) or not authorization[len(prefix) :]:
        return None
    service = AcpAuthorizationService(token_store)
    try:
        if server_scope:
            return service.resolve_server_scope(authorization[len(prefix) :])
        return service.validate_or_refresh(authorization[len(prefix) :])
    except AcpAuthorizationDenied:
        return None


class AcpAuthorizationService:
    def __init__(
        self,
        token_store: TokenStore,
        *,
        token_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._tokens = token_store
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))

    def prepare_grant(
        self,
        *,
        auth_token: str,
        project_id: str,
        workspace: str,
        launch_id: Optional[str] = None,
        path_format: Optional[str] = None,
        managed_protocol_version: Optional[str] = None,
    ) -> PreparedAcpGrant:
        """Mint a one-time launch grant for an authenticated plugin.

        Any grant previously issued for this plugin session is discarded first,
        so re-preparing (which the plugin does on every startup and login)
        leaves exactly one usable grant rather than accumulating valid tokens.
        """
        managed_values = (launch_id, path_format, managed_protocol_version)
        if any(value is not None for value in managed_values) and not all(
            value is not None for value in managed_values
        ):
            raise AcpAuthorizationDenied(
                "Managed launch identity, path format, and protocol are required together."
            )
        if managed_protocol_version is not None and managed_protocol_version != "1":
            raise AcpAuthorizationDenied("Unsupported managed protocol version.")
        canonical_workspace = (
            self._validate_client_workspace(workspace, path_format)
            if path_format is not None
            else self._canonical_workspace(workspace)
        )
        state = self._active_plugin_scope(
            auth_token=auth_token,
            project_id=project_id,
            workspace=canonical_workspace,
            already_canonical=True,
        )
        if launch_id is not None:
            state["launch_id"] = launch_id
        if path_format is not None:
            state["path_format"] = path_format
        if managed_protocol_version is not None:
            state["managed_protocol_version"] = managed_protocol_version
        pending_key = self._pending_grant_key(state, launch_id)
        pending = self._tokens.get("acp_pending_grant", pending_key)
        if pending is not None and pending.get("grant"):
            pending_grant = pending["grant"]
            pending_state = self._tokens.get("acp_grant", pending_grant)
            if launch_id is not None and pending_state == {
                **state,
                "pending_grant_key": pending_key,
            }:
                # A bridge request can be retried after its HTTP response is
                # lost. Return the still-live grant for this exact launch so
                # two concurrent responses cannot invalidate each other.
                return PreparedAcpGrant(
                    grant=pending_grant,
                    workspace=state["workspace"],
                    launch_id=launch_id,
                    path_format=path_format,
                    managed_protocol_version=managed_protocol_version,
                )
            self._tokens.discard("acp_grant", pending_grant)
        grant = self._token_factory()
        state["pending_grant_key"] = pending_key
        self._tokens.set("acp_grant", grant, state, force_reset_exp=True)
        self._tokens.set(
            "acp_pending_grant",
            pending_key,
            {"grant": grant},
            force_reset_exp=True,
        )
        return PreparedAcpGrant(
            grant=grant,
            workspace=state["workspace"],
            launch_id=launch_id,
            path_format=path_format,
            managed_protocol_version=managed_protocol_version,
        )

    def exchange_grant(self, grant: str) -> AcpSessionAuthorization:
        """Trade a launch grant for an agent session token.

        The scope is re-checked *before* consuming, then the grant is consumed
        atomically — so a grant whose parent session died in the meantime is
        rejected, and a valid grant can only ever be redeemed once.
        """
        state = self._tokens.get("acp_grant", grant)
        if state is None:
            raise AcpAuthorizationDenied(
                "ACP grant is invalid or has already been used."
            )
        self._require_active_scope(state)
        consumed_state = self._tokens.consume("acp_grant", grant)
        if consumed_state is None:
            raise AcpAuthorizationDenied(
                "ACP grant is invalid or has already been used."
            )
        pending_key = consumed_state.get("pending_grant_key")
        if pending_key:
            self._tokens.discard("acp_pending_grant", pending_key)
        acp_token = self._token_factory()
        self._tokens.set("acp_session", acp_token, consumed_state, force_reset_exp=True)
        return AcpSessionAuthorization(
            acp_token=acp_token,
            user_id=consumed_state["user_id"],
            project_id=consumed_state["project_id"],
            workspace=consumed_state["workspace"],
        )

    def validate_or_refresh(self, acp_token: str) -> AcpSessionAuthorization:
        state = self._validated_session_state(acp_token)
        return AcpSessionAuthorization(
            acp_token=acp_token,
            user_id=state["user_id"],
            project_id=state["project_id"],
            workspace=state["workspace"],
        )

    def resolve_server_scope(self, acp_token: str) -> AcpServerAuthorization:
        state = self._validated_session_state(acp_token)
        project_info = self._tokens.get("project_token", state["project_id"])
        if project_info is None:
            raise AcpAuthorizationDenied("An active project association is required.")
        return AcpServerAuthorization(
            acp_token=acp_token,
            user_id=state["user_id"],
            session_id=state["parent_session_token"],
            project_id=state["project_id"],
            project_info=project_info,
            workspace=state["workspace"],
        )

    def _validated_session_state(self, acp_token: str) -> dict[str, str]:
        """Revalidate an agent session and slide its expiry.

        A session whose parent plugin login has gone away is *discarded* here,
        not merely rejected — otherwise it would sit in Redis until its TTL,
        still resolvable if the user logged back in.
        """
        state = self._tokens.get("acp_session", acp_token)
        if state is None:
            raise AcpAuthorizationDenied("ACP session is invalid or expired.")
        try:
            self._require_active_scope(state)
        except AcpAuthorizationDenied:
            self._tokens.discard("acp_session", acp_token)
            raise
        # Touch the parent session so an actively-working agent keeps the user's
        # IDE session alive, matching how plugin requests behave.
        self._tokens.get("session_token", state["parent_session_token"], reset_exp=True)
        self._tokens.set("acp_session", acp_token, state, force_reset_exp=True)
        return state

    def _active_plugin_scope(
        self,
        *,
        auth_token: str,
        project_id: str,
        workspace: str,
        already_canonical: bool = False,
    ) -> dict[str, str]:
        auth_info = self._tokens.get("auth_token", auth_token)
        if auth_info is None or not auth_info.get("user_id"):
            raise AcpAuthorizationDenied("An authenticated plugin login is required.")
        user_id = auth_info["user_id"]
        user_info = self._tokens.get("user_token", user_id)
        if user_info is None or not user_info.get("session_token"):
            raise AcpAuthorizationDenied("An active plugin session is required.")
        # Managed grants arrive with a client-canonicalized workspace (validated
        # against the participant OS without touching the server filesystem).
        # Re-resolving that value with the server OS would reject Windows drive
        # paths on a Linux backend, so already-canonical values are preserved.
        canonical = workspace if already_canonical else self._canonical_workspace(workspace)
        return self._bound_scope(
            auth_token=auth_token,
            user_id=user_id,
            session_token=user_info["session_token"],
            project_id=project_id,
            workspace=canonical,
        )

    def _require_active_scope(self, state: dict[str, str]) -> None:
        """Re-check that everything the grant was bound to is still valid."""
        auth_info = self._tokens.get("auth_token", state["parent_auth_token"])
        if auth_info is None or auth_info.get("user_id") != state["user_id"]:
            raise AcpAuthorizationDenied("The parent plugin login is no longer active.")
        user_info = self._tokens.get("user_token", state["user_id"])
        if (
            user_info is None
            or user_info.get("session_token") != state["parent_session_token"]
        ):
            raise AcpAuthorizationDenied(
                "The parent plugin session is no longer active."
            )
        self._bound_scope(
            auth_token=state["parent_auth_token"],
            user_id=state["user_id"],
            session_token=state["parent_session_token"],
            project_id=state["project_id"],
            workspace=state["workspace"],
        )

    def _bound_scope(
        self,
        *,
        auth_token: str,
        user_id: str,
        session_token: str,
        project_id: str,
        workspace: str,
    ) -> dict[str, str]:
        """Verify the session↔project association in both directions.

        Checking only one direction would let a stale association on either side
        authorize an agent against a project the session no longer has open.
        """
        session_info = self._tokens.get("session_token", session_token)
        if session_info is None or project_id not in session_info.get(
            "project_tokens", []
        ):
            raise AcpAuthorizationDenied("An active project association is required.")
        project_info = self._tokens.get("project_token", project_id)
        if project_info is None or session_token not in project_info.get(
            "session_tokens", []
        ):
            raise AcpAuthorizationDenied("An active project association is required.")
        return {
            "user_id": user_id,
            "parent_auth_token": auth_token,
            "parent_session_token": session_token,
            "project_id": project_id,
            "workspace": workspace,
        }

    @staticmethod
    def _canonical_workspace(workspace: str) -> str:
        """Resolve the workspace to a canonical absolute path.

        The agent's file tools confine themselves to this directory, so it must
        be normalized before being bound into the grant — otherwise a relative
        or ``..``-containing path could widen the agent's reach.
        """
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            raise AcpAuthorizationDenied("A canonical absolute workspace is required.")
        return path.resolve().as_posix()

    @staticmethod
    def _validate_client_workspace(workspace: str, path_format: str) -> str:
        """Validate an already-canonical client path without using the server OS."""
        if path_format not in {"windows", "posix"}:
            raise AcpAuthorizationDenied("Unsupported client path format.")
        path_cls = PureWindowsPath if path_format == "windows" else PurePosixPath
        path = path_cls(workspace)
        if not path.is_absolute() or ".." in path.parts:
            raise AcpAuthorizationDenied("A canonical absolute workspace is required.")
        return str(path)

    @staticmethod
    def _pending_grant_key(state: dict[str, str], launch_id: Optional[str]) -> str:
        # Legacy callers retain the old one-pending-grant-per-plugin-session
        # behavior. Managed callers isolate retries and concurrent project launches.
        if launch_id is None:
            return state["parent_session_token"]
        return (
            f"{state['parent_session_token']}:{state['project_id']}:"
            f"{state['workspace']}:{launch_id}"
        )
