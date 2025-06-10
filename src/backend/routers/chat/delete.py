import logging
from typing import Union
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteChatError,
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    NoAccessToGetQueryError,
    QueryNotFoundError,
)
from response_models import DeleteChatSuccessResponse

router = APIRouter()


@router.delete(
    "/{chat_id}",
    responses={
        "200": {"model": DeleteChatSuccessResponse},
        "401": {
            "model": Union[
                InvalidOrExpiredAuthToken,
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "403": {"model": NoAccessToGetQueryError},
        "404": {"model": QueryNotFoundError},
        "429": {"model": ErrorResponse},
        "500": {"model": DeleteChatError},
    },
)
def delete_chat(
    chat_id: UUID,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
    project_token: str = Cookie("project_token", default=None),
) -> JsonResponseWithStatus:
    """
    Delete a specific chat by its ID.
    Validates that the user has access to the chat through their session and project tokens.
    """
    logging.info(f"Attempting to delete chat with ID: {chat_id}")
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Get session info from Redis
        # Validate session token
        session_info = redis_manager.get("session_token", session_token)
        if session_token is None or session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Get user_id and auth_token from session info
        auth_token = session_info.get("auth_token")
        if not auth_token:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredAuthToken()
            )
        user_id = auth_info.get("user_id")
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Validate project token
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Verify project is linked to this session
        session_projects = session_info.get("project_tokens", [])
        if project_token not in session_projects:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Verify that the chat exists and belongs to this user and project
        chat = crud.get_chat_by_id(db=db_session, chat_id=chat_id)
        if chat is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        # Verify the chat belongs to the correct user and project
        if str(chat.user_id) != user_id or str(chat.project_id) != project_token:
            return JsonResponseWithStatus(
                status_code=403,
                content=NoAccessToGetQueryError(),
            )

        # Delete the chat with cascade
        success = crud.delete_chat_cascade(db=db_session, chat_id=chat_id)

        if not success:
            return JsonResponseWithStatus(
                status_code=500,
                content=DeleteChatError(),
            )

        return JsonResponseWithStatus(
            status_code=200,
            content=DeleteChatSuccessResponse(),
        )

    except Exception as e:
        logging.error(f"Error deleting chat {chat_id}: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=DeleteChatError(),
        )
