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
    NoAccessToGetQueryError,
    QueryNotFoundError,
    RetrieveCompletionsError,
)
from response_models import (
    CompletionErrorItem,
    ResponseCompletionItem,
    ResponseCompletionResponseData,
)

router = APIRouter()


@router.get(
    "/{query_id}",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "403": {"model": NoAccessToGetQueryError},
        "404": {"model": QueryNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": RetrieveCompletionsError},
    },
)
def get_completions_by_query(
    query_id: UUID,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Retrieve code completions associated with a specific query ID.

    This endpoint validates the user's session token and authorization,
    ensures the requested query exists and belongs to the requesting user,
    and then fetches all the associated completion generations from the database.

    Parameters:
    - query_id (UUID): The unique identifier for the meta query whose completions are requested.
    - app (App, dependency): The application instance providing database and Redis access.
    - session_token (str, cookie): The session token cookie for user authentication.

    Returns:
    - JsonResponseWithStatus: JSON response with HTTP status and either the completions data
      or an error response detailing the failure reason.

    Possible responses:
    - 200: Successfully retrieved completions for the given query ID.
    - 401: Session token is invalid, expired, or missing.
    - 403: User does not have access rights to the requested query.
    - 404: The requested query ID does not exist.
    - 422, 429: Various client errors (validation or rate limiting).
    - 500: Internal server error while retrieving completions.
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
        # Check if the meta query exists in DB
        query = crud.get_meta_query_by_id(db_session, query_id)
        if not query:
            return JsonResponseWithStatus(
                status_code=404,
                content=QueryNotFoundError(),
            )

        # Verify user owns the query (convert user_id from Redis to string for comparison)
        if str(query.user_id) != user_id:
            return JsonResponseWithStatus(
                status_code=403, content=NoAccessToGetQueryError()
            )

        # Retrieve completion generations linked to the meta query
        generations = crud.get_generations_by_meta_query_id(db_session, str(query_id))

        # Construct the list of completions to return
        completions = []
        for generation in generations:
            model = crud.get_model_by_id(db_session, int(str(generation.model_id)))

            if not model:
                completions.append(
                    CompletionErrorItem(model_name=f"Model ID: {generation.model_id}")
                )
            else:
                completions.append(
                    ResponseCompletionItem(
                        model_id=int(str(generation.model_id)),
                        model_name=str(model.model_name) if model else "Unknown Model",
                        completion=str(generation.completion),
                        generation_time=int(str(generation.generation_time)),
                        confidence=float(str(generation.confidence)),
                    )
                )

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                data=ResponseCompletionResponseData(
                    meta_query_id=query_id,
                    completions=completions,
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error retrieving completions: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=RetrieveCompletionsError(),
        )
