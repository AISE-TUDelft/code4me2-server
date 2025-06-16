import logging
import uuid
from typing import Union

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries
from App import App
from backend.Responses import (
    ActivateProjectError,
    ActivateProjectPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    ProjectNotFoundError,
    SessionNotFoundError,
    UserNotFoundError,
)

router = APIRouter()


@router.put(
    "",
    response_model=ActivateProjectPostResponse,
    responses={
        "200": {"model": ActivateProjectPostResponse},
        "401": {
            "model": Union[InvalidOrExpiredSessionToken, InvalidOrExpiredAuthToken]
        },
        "404": {
            "model": Union[
                UserNotFoundError, SessionNotFoundError, ProjectNotFoundError
            ]
        },
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ActivateProjectError},
    },
)
def activate_project(
    activate_project_request: Queries.ActivateProject,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Activate a project for a user session by performing the following:

    1. Validate the auth token and session token from cookies.
    2. If the project exists in Redis, update its expiration time.
    3. If the project is not found in Redis, fetch it from the database and cache it.
    4. Associate the project with the user session and user in the database if not already linked.
    5. Return a success response with the project token set as an HttpOnly cookie.

    Args:
        activate_project_request: Request data containing the project ID to activate.
        app: FastAPI dependency injection to get app context.
        auth_token: Auth token from cookie to validate user identity.

    Returns:
        JsonResponseWithStatus: Success or error response with appropriate status and messages.
    """
    redis_manager = app.get_redis_manager()
    db_session = app.get_db_session()
    config = app.get_config()
    try:
        auth_info = redis_manager.get("auth_token", auth_token)
        # Validate auth token presence and associated user_id
        if auth_info is None or not auth_info.get("user_id"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredAuthToken(),
            )

        user_id = auth_info["user_id"]
        if crud.get_user_by_id(db_session, uuid.UUID(user_id)) is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=UserNotFoundError(),
            )

        user_info = redis_manager.get("user_token", user_id)
        if user_info is None or not user_info.get("session_token"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        session_token = user_info.get("session_token")
        if crud.get_session_by_id(db_session, uuid.UUID(session_token)) is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=SessionNotFoundError(),
            )
        # Retrieve session info from Redis
        session_info = redis_manager.get("session_token", session_token)
        # Validate session token presence and validity
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        project_token = str(activate_project_request.project_id)
        # Attempt to get project info from Redis
        project_info = redis_manager.get("project_token", project_token)
        logging.log(logging.INFO, f"Retrieved project info: {project_info}")

        if not project_info:
            # Project not in Redis, fetch from database
            existing_project = crud.get_project_by_id(
                db_session, uuid.UUID(project_token)
            )
            if existing_project is None:
                # Project token invalid or expired
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredProjectToken(),
                )
            else:
                # Cache project info in Redis with relevant metadata
                redis_manager.set(
                    "project_token",
                    project_token,
                    {
                        "session_tokens": [session_token],
                        "multi_file_contexts": existing_project.multi_file_contexts,
                        "multi_file_context_changes": existing_project.multi_file_context_changes,
                    },
                )
        elif crud.get_project_by_id(db_session, uuid.UUID(project_token)) is None:
            # Project token invalid or expired
            return JsonResponseWithStatus(
                status_code=404,
                content=ProjectNotFoundError(),
            )

        # Add the project token to the session's project list in Redis
        session_projects = session_info.get("project_tokens", [])
        session_projects.append(project_token)
        session_info["project_tokens"] = session_projects
        redis_manager.set("session_token", session_token, session_info)

        # Ensure session-project link exists in database
        if not crud.get_session_project(
            db_session, uuid.UUID(session_token), uuid.UUID(project_token)
        ):
            crud.create_session_project(
                db_session,
                Queries.CreateSessionProject(
                    session_id=uuid.UUID(session_token),
                    project_id=uuid.UUID(project_token),
                ),
            )

        # Ensure user-project link exists in database
        if not crud.get_user_project(
            db_session, uuid.UUID(user_id), uuid.UUID(project_token)
        ):
            crud.create_user_project(
                db_session,
                Queries.CreateUserProject(
                    project_id=uuid.UUID(project_token), user_id=uuid.UUID(user_id)
                ),
            )

        # Return success response with project token set as cookie
        response_obj = JsonResponseWithStatus(
            status_code=200, content=ActivateProjectPostResponse()
        )
        response_obj.set_cookie(
            key="project_token",
            value=project_token,
            httponly=True,
            samesite="lax",
        )
        return response_obj
    except Exception as e:
        logging.log(logging.ERROR, f"Error activating project: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=ActivateProjectError(),
        )


def __init__():
    """
    Module-level initializer placeholder.

    This function is executed when the module is imported.
    """
    pass
