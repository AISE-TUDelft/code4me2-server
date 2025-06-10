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
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": CreateProjectError},
    },
)
def create_project(
    project_to_create: Queries.CreateProject,
    app: App = Depends(App.get_instance),
    auth_token: str = Cookie("auth_token"),
) -> JsonResponseWithStatus:
    """
    Create a new project
    1. Validate the provided session token
    2. If valid, create a project and return the project token
    3. If invalid, return an appropriate error response
    """
    redis_manager = app.get_redis_manager()
    config = app.get_config()
    db_session = app.get_db_session()

    try:
        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredAuthToken()
            )
        session_token = auth_info.get("session_token", "")
        user_id = auth_info.get("user_id", "")
        session_info = redis_manager.get("session_token", session_token)
        # Validate session token
        if session_token == "" or session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        logging.log(logging.INFO, f"Creating project for user_id: {user_id}")

        # Create project
        created_project = crud.create_project(db_session, project_to_create)
        project_token = str(created_project.project_id)
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

        redis_manager.set(
            "project_token",
            project_token,
            {
                "session_tokens": [session_token],
                "multi_file_contexts": {},
                "multi_file_context_changes": {},
            },
        )
        session_projects = session_info.get("project_tokens", [])
        session_projects.append(project_token)
        session_info["project_tokens"] = session_projects
        redis_manager.set("session_token", session_token, session_info)

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
        logging.log(logging.ERROR, f"Error creating project: {e}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=CreateProjectError(),
        )


def __init__():
    """
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
