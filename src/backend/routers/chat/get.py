import logging
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    NoAccessToGetQueryError,
    QueryNotFoundError,
    RetrieveChatCompletionsError,
)
from backend.routers.chat.request import get_chat_history_response
from response_models import (
    ChatHistoryResponse,
)

router = APIRouter()


@router.get(
    "/{chat_id}",
    responses={
        "200": {"model": ChatHistoryResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "403": {"model": NoAccessToGetQueryError},
        "404": {"model": QueryNotFoundError},
        "429": {"model": ErrorResponse},
        "500": {"model": RetrieveChatCompletionsError},
    },
)
def get_chat_history(
    chat_id: UUID,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Get the complete chat history for a specific chat ID.
    """
    logging.info(f"Getting chat history for chat: {chat_id}")
    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Get session info from Redis
        session_info = redis_manager.get("session_token", session_token)
        if session_token is None or session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Get auth token from session info and then get user_id from auth token
        auth_token = session_info.get("auth_token")
        if not auth_token:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        auth_info = redis_manager.get("auth_token", auth_token)
        if auth_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        user_id = auth_info.get("user_id")
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Check if chat exists
        chat = crud.get_chat_by_id(db_session, chat_id)
        if not chat:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        # Check if user has access to this chat
        if str(chat.user_id) != user_id:
            return JsonResponseWithStatus(
                status_code=403, content=NoAccessToGetQueryError()
            )

        # Get chat history
        chat_history = get_chat_history_response(db_session, chat_id, user_id)
        if not chat_history:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        return JsonResponseWithStatus(
            status_code=200,
            content=chat_history,
        )

    except Exception as e:
        logging.error(f"Error retrieving chat history: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=RetrieveChatCompletionsError(),
        )
