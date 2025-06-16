"""
Route to delete a chat by ID.

This endpoint allows a user to delete a specific chat instance identified by its `chat_id`.
It performs several layers of access validation using tokens stored in Redis:
- Validates the session token and retrieves associated authentication information.
- Checks if the project token is valid and associated with the current session.
- Ensures that the chat to be deleted belongs to the authenticated user and project.

If all validations pass, the chat and its related data are deleted from the database.
Appropriate HTTP status codes and error messages are returned in case of invalid tokens,
unauthorized access, or server/database errors.
"""

import logging
from typing import Union
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    DeleteChatError,
    ErrorResponse,
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
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "403": {"model": NoAccessToGetQueryError},
        "404": {"model": QueryNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": DeleteChatError},
    },
)
def delete_chat(
    chat_id: UUID,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Delete a specific chat by its ID.
    Validates that the user has access to the chat through their session and project tokens.
    """
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Validate session token
        session_info = redis_manager.get("session_token", session_token)
        if session_info is None or not session_info.get("user_token"):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )
        user_id = session_info.get("user_token")
        # Skipped checking if the user exists in the database

        # Validate project token
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Ensure project token is linked to session
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
