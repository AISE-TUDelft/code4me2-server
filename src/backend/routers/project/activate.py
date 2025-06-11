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
)

router = APIRouter()


@router.put(
    "/",
    response_model=ActivateProjectPostResponse,
    responses={
        "200": {"model": ActivateProjectPostResponse},
        "401": {
            "model": Union[InvalidOrExpiredSessionToken, InvalidOrExpiredAuthToken]
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
    Activates the project by following these steps:
    1. Validate the provided auth token
    2. If valid, return confirmation
    3. If invalid, return an appropriate error response
    4. The project might exist in redis or in the database, if it is in the database, it should be fetched from there and put in redis
    if it is in the redis, its expiration time should be updated.

    """
    redis_manager = app.get_redis_manager()
    db_session = app.get_db_session()
    config = app.get_config()
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
        if not session_token or session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        project_token = str(activate_project_request.project_id)
        project_info = redis_manager.get("project_token", project_token)
        logging.log(logging.INFO, f"Retrieved project info: {project_info}")
        if not project_info:
            # The project is not in the redis, so we need to fetch it from the database if it exists there
            existing_project = crud.get_project_by_id(
                db_session, uuid.UUID(project_token)
            )
            if existing_project is None:
                return JsonResponseWithStatus(
                    status_code=401,
                    content=InvalidOrExpiredProjectToken(),
                )
            else:
                redis_manager.set(
                    "project_token",
                    project_token,
                    {
                        "session_tokens": [session_token],
                        "multi_file_contexts": existing_project.multi_file_contexts,
                        "multi_file_context_changes": existing_project.multi_file_context_changes,
                    },
                )
        session_projects = session_info.get("project_tokens", [])
        session_projects.append(project_token)
        session_info["project_tokens"] = session_projects
        redis_manager.set("session_token", session_token, session_info)

        crud.create_session_project(
            db_session,
            Queries.CreateSessionProject(
                session_id=uuid.UUID(session_token), project_id=uuid.UUID(project_token)
            ),
        )

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
    This function is called when the module is imported.
    It is used to initialize the module and import the router.
    """
    pass
