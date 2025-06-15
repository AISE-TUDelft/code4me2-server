import logging
import uuid
from typing import Union

from fastapi import APIRouter, Cookie, Depends

import Queries
from App import App
from backend.Responses import (
    CreateProjectError,
    CreateProjectPostResponse,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    SessionNotFoundError,
    UserNotFoundError,
)
from database import crud

router = APIRouter()


@router.post(
    "",
    response_model=CreateProjectPostResponse,
    responses={
        "201": {"model": CreateProjectPostResponse},
        "401": {
            "model": Union[InvalidOrExpiredAuthToken, InvalidOrExpiredSessionToken]
        },
        "404": {"model": Union[UserNotFoundError, SessionNotFoundError]},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": CreateProjectError},
    },
)
def create_project(
    project_to_create: Queries.CreateProject,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Create a new project for an authenticated user.

    Steps:
    1. Validate the provided auth token to get session info.
    2. Verify the session token is valid.
    3. Create a new project in the database.
    4. Associate the project with the session and the user.
    5. Store project metadata in Redis.
    6. Return the project token as an HttpOnly cookie in the response.

    Args:
        project_to_create: Project data provided in the request body.
        app: FastAPI dependency to access the app context.
        auth_token: Auth token passed as a cookie for authentication.

    Returns:
        JsonResponseWithStatus: JSON response with project token or error details.
    """
    redis_manager = app.get_redis_manager()
    config = app.get_config()
    db_session = app.get_db_session()

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

        # Create the project in the database
        created_project = crud.create_project(db_session, project_to_create)
        project_token = str(created_project.project_id)
        # Link the new project with the session and user in the database
        crud.create_session_project(
            db_session,
            Queries.CreateSessionProject(
                session_id=uuid.UUID(session_token), project_id=uuid.UUID(project_token)
            ),
        )
        crud.create_user_project(
            db_session,
            Queries.CreateUserProject(
                project_id=uuid.UUID(project_token), user_id=user_id
            ),
        )

        # Store project token metadata in Redis
        redis_manager.set(
            "project_token",
            project_token,
            {
                "session_tokens": [session_token],
                "multi_file_contexts": {},
                "multi_file_context_changes": {},
            },
        )
        # Update session info with new project token
        session_projects = session_info.get("project_tokens", [])
        session_projects.append(project_token)
        session_info["project_tokens"] = session_projects
        redis_manager.set("session_token", session_token, session_info)

        # Create response with the project token cookie
        response_obj = JsonResponseWithStatus(
            status_code=201,
            content=CreateProjectPostResponse(project_token=project_token),
        )
        response_obj.set_cookie(
            key="project_token",
            value=project_token,
            httponly=True,
            samesite="lax",
        )
        return response_obj
    except Exception as e:
        logging.error(f"Error creating project: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=CreateProjectError(),
        )


def __init__():
    """
    Module-level initializer placeholder.

    This function runs when the module is imported and
    can be used for module setup if necessary.
    """
    pass
