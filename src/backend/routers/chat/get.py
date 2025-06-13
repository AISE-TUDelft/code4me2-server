import logging
from typing import Union
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    ErrorResponse,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    NoAccessToGetQueryError,
    QueryNotFoundError,
    RetrieveChatCompletionsError,
)
from response_models import (
    ChatCompletionItem,
    ChatHistoryItem,
    ChatHistoryResponse,
    ChatHistoryResponsePage,
    ChatMessageItem,
    ChatMessageRole,
)

# Create a FastAPI router for chat history endpoints
router = APIRouter()


@router.get(
    "/{page_number}",
    responses={
        "200": {"model": ChatHistoryResponsePage},
        "401": {
            "model": Union[
                InvalidOrExpiredAuthToken,
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "403": {"model": NoAccessToGetQueryError},
        "404": {"model": QueryNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": RetrieveChatCompletionsError},
    },
)
def get_chat_history(
    app: App = Depends(App.get_instance),
    page_number: int = 1,
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Retrieve a page of chat history for a specific project associated with the current session.

    Parameters:
    - app (App): Dependency-injected FastAPI application instance.
    - page_number (int): The page number of the chat history to retrieve.
    - session_token (str): Session token stored in a cookie.
    - project_token (str): Project token stored in a cookie.

    Returns:
    - JsonResponseWithStatus: Paginated chat history or an error response.
    """
    logging.info(
        f"Attempting to retrieve page {page_number} of chat history for the user's project."
    )

    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Step 1: Validate session token
        session_info = redis_manager.get("session_token", session_token)
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Step 2: Extract and validate auth token
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

        # Step 3: Validate project token
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Step 4: Ensure the project is linked to this session
        session_projects = session_info.get("project_tokens", [])
        if project_token not in session_projects:
            return JsonResponseWithStatus(
                status_code=401, content=InvalidOrExpiredProjectToken()
            )

        # Step 5: Fetch chat history from the database
        chat_history = crud.get_project_chat_history(
            db=db_session,
            user_id=user_id,
            project_id=UUID(project_token),
            page_number=page_number,
        )

        if chat_history is None:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        # Step 6: Return parsed response
        return JsonResponseWithStatus(
            status_code=200,
            content=ChatHistoryResponsePage(
                items=__parse_chat_history(db_session, chat_history),
                per_page=10,
                page=page_number,
            ),
        )

    except Exception as e:
        logging.error(f"Error retrieving chat history: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=RetrieveChatCompletionsError(),
        )


def __parse_chat_history(db_session, chat_history_data):
    """
    Parse raw chat history data from the database into a structured response format.

    Parameters:
    - db_session: SQLAlchemy database session.
    - chat_history_data: Raw chat and generation data fetched from the DB.

    Returns:
    - List[ChatHistoryResponse]: List of chat sessions with corresponding conversation history.
    """
    if not chat_history_data:
        return []

    parsed_history = []

    for chat, chat_conversations in chat_history_data:
        # Extract high-level chat metadata
        chat_id = chat.chat_id
        title = chat.title
        history_items = []

        for meta_query, context, generations in chat_conversations:
            # Construct user message from context
            user_message = ChatMessageItem(
                role=ChatMessageRole.USER,
                content=context.prefix,
                timestamp=meta_query.timestamp,
                meta_query_id=meta_query.meta_query_id,
            )

            # Construct assistant responses from generations
            assistant_responses = []
            for generation in generations:
                model = crud.get_model_by_id(db_session, generation.model_id)
                model_name = (
                    model.model_name if model else f"Model ID: {generation.model_id}"
                )

                assistant_responses.append(
                    ChatCompletionItem(
                        model_id=generation.model_id,
                        model_name=model_name,
                        completion=generation.completion,
                        generation_time=generation.generation_time,
                        confidence=generation.confidence,
                        was_accepted=generation.was_accepted,
                    )
                )

            # Build complete conversation item
            history_item = ChatHistoryItem(
                user_message=user_message,
                assistant_responses=assistant_responses,
            )
            history_items.append(history_item)

        # Append parsed chat history
        chat_response = ChatHistoryResponse(
            chat_id=chat_id,
            title=title,
            history=history_items,
        )
        parsed_history.append(chat_response)

    return parsed_history
