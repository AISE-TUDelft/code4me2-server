import logging
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    CompletionPostResponse,
    ErrorResponse,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    QueryNotFoundError,
    RetrieveCompletionsError,
)
from response_models import ResponseCompletionItem, ResponseCompletionResponseData

router = APIRouter()


@router.get(
    "/{query_id}",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "404": {"model": QueryNotFoundError},
        "429": {"model": ErrorResponse},
        "500": {"model": RetrieveCompletionsError},
    },
)
def get_completions_by_query(
    query_id: UUID,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Get completions for a specific query ID.
    """
    logging.info(f"Getting completions for query: {query_id}")
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

        # check if metaquery exists and if so if the user is the owner of the query
        query = crud.get_meta_query_by_id(db_session, str(query_id))
        if not query:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        # Convert user_id from string (Redis) to UUID for comparison with database user_id (UUID)
        if query.user_id != UUID(user_id):
            return JsonResponseWithStatus(
                status_code=403,
                content=ErrorResponse(
                    message="You do not have permission to access this query."
                ),
            )

        # Retrieve completions for the query
        generations = crud.get_generations_by_meta_query_id(db_session, str(query_id))

        # Build response
        completions = []
        for generation in generations:
            model = crud.get_model_by_id(db_session, generation.model_id)

            completions.append(
                ResponseCompletionItem(
                    model_id=generation.model_id,
                    model_name=model.model_name if model else "Unknown Model",
                    completion=generation.completion,
                    generation_time=generation.generation_time,
                    confidence=generation.confidence,
                )
            )

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                data=ResponseCompletionResponseData(
                    query_id=query_id, completions=completions
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error retrieving completions: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=RetrieveCompletionsError(),
        )
