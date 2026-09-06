"""Shared FastAPI dependency for ACP-bearer-authenticated agent endpoints.

The built-in ``code4me2-agent`` runtime is a separate local process with no
session cookie, so it authenticates with the ``acp_session`` bearer token it got
from the grant exchange (see ``backend.acp_authorization``). This module turns
that header into a resolved scope for route handlers.

Every failure mode collapses to one flat 401 so the response never reveals
*which* check failed.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException

from App import App
from backend.acp_authorization import (
    AcpServerAuthorization,
    AcpSessionAuthorization,
    authorize_acp_bearer,
)

_UNAUTHORIZED_DETAIL = "ACP authorization is invalid or expired."


def require_acp_scope(
    app: App = Depends(App.get_instance),
    authorization: str = Header(default=""),
) -> AcpSessionAuthorization:
    """Resolve ``Authorization: Bearer <acp_token>`` to a session scope.

    The returned scope's ``user_id`` / ``project_id`` / ``workspace`` are
    server-derived from the original grant, so handlers can treat them as
    trusted and must never take those values from the request body instead.
    """
    scope = authorize_acp_bearer(app.get_redis_manager(), authorization)
    if not isinstance(scope, AcpSessionAuthorization):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)
    return scope


def require_acp_server_scope(
    app: App = Depends(App.get_instance),
    authorization: str = Header(default=""),
) -> AcpServerAuthorization:
    """Same, but also resolves the project record.

    For handlers that need the project's stored context, not just its id.
    """
    scope = authorize_acp_bearer(
        app.get_redis_manager(), authorization, server_scope=True
    )
    if not isinstance(scope, AcpServerAuthorization):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)
    return scope


def optional_acp_scope(
    app: App = Depends(App.get_instance),
    authorization: str = Header(default=""),
) -> Optional[AcpSessionAuthorization]:
    """Resolve the scope if present, else None — for endpoints valid either way."""
    scope = authorize_acp_bearer(app.get_redis_manager(), authorization)
    return scope if isinstance(scope, AcpSessionAuthorization) else None
