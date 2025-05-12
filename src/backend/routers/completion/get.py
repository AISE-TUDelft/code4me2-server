import logging
from uuid import UUID

from fastapi import APIRouter, Depends

import database.crud as crud
from App import App
from backend.models.Responses import (
    CompletionPostResponse,
    ErrorResponse,
    JsonResponseWithStatus,
)
from base_models import CompletionItem, CompletionResponseData

router = APIRouter()


@router.get(
    "/{query_id}",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "404": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
)
def get_completions_by_query(
    query_id: UUID,
    app: App = Depends(App.get_instance),
) -> JsonResponseWithStatus:
    """
    Get completions for a specific query ID.
    """
    logging.log(logging.INFO, f"Getting completions for query: {query_id}")
    db_session = app.get_db_session()

    try:
        # Check if query exists
        query = crud.get_query_by_id(db_session, str(query_id))
        if not query:
            return JsonResponseWithStatus(
                status_code=404, content=ErrorResponse(message="Query not found")
            )

        # Get all generations for this query
        generations = crud.get_generations_by_query_id(db_session, str(query_id))
        if not generations:
            return JsonResponseWithStatus(
                status_code=404,
                content=ErrorResponse(message="No completions found for this query"),
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
                    confidence=generation.confidence,
                )
            )

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                message="Completions retrieved successfully",
                data=CompletionResponseData(query_id=query_id, completions=completions),
            ),
        )

    except Exception as e:
        logging.error(f"Error retrieving completions: {str(e)}")
        return JsonResponseWithStatus(
            status_code=500,
            content=ErrorResponse(message=f"Failed to retrieve completions: {str(e)}"),
        )
