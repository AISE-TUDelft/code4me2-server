import logging
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    CompletionPostResponse,
    CompletionsNotFoundError,
    ErrorResponse,
    InvalidSessionToken,
    JsonResponseWithStatus,
    QueryNotFoundError,
    RetrieveCompletionsError,
)
from base_models import CompletionItem, CompletionResponseData

router = APIRouter()


@router.get(
    "/{query_id}",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {"model": InvalidSessionToken},
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
    logging.log(logging.INFO, f"Getting completions for query: {query_id}")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()

    try:
        # TODO: We should change the structure of this function to also ask for user_id (it's better to define a new Query class for getting queries which has user_id and query_id) and then only return the queries of the current user
        # or we can simply change get_query_by_id to get_query_by_id_for_user to only return the queries of the current user. For now we can assume query_ids are unique and secure enough for each user.
        # Check if user is authenticated
        user_dict = session_manager.get_session(session_token)
        if session_token is None or user_dict is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidSessionToken(),
            )
        # Check if query exists
        query = crud.get_query_by_id(db_session, str(query_id))
        if not query:
            return JsonResponseWithStatus(status_code=404, content=QueryNotFoundError())

        # Get all generations for this query
        generations = crud.get_generations_by_query_id(db_session, str(query_id))
        if not generations:
            return JsonResponseWithStatus(
                status_code=404,
                content=CompletionsNotFoundError(),
            )

        # Build response
        completions = []
        for generation in generations:
            model = crud.get_model_by_id(db_session, generation.model_id)

            completions.append(
                CompletionItem(
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
                data=CompletionResponseData(query_id=query_id, completions=completions),
            ),
        )

    except Exception as e:
        logging.error(f"Error retrieving completions: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=RetrieveCompletionsError(str(e)),
        )
