import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends

import backend.completion as completion
import database.crud as crud
from App import App
from backend.Responses import (
    CompletionPostResponse,
    ErrorResponse,
    InvalidSessionToken,
    JsonResponseWithStatus,
)
from base_models import CompletionItem, CompletionResponseData
from Queries import (
    CreateContext,
    CreateGeneration,
    CreateQuery,
    CreateTelemetry,
    RequestCompletion,
)

router = APIRouter()


@router.post(
    "/",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {"model": InvalidSessionToken},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
)
def request_completion(
    completion_request: RequestCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Request code completions based on provided context.
    """
    logging.log(logging.INFO, f"Completion request: {completion_request}")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()
    completion_models = app.get_completion_models()

    try:
        # Check if user is authenticated
        user_dict = session_manager.get_session(session_token)
        if session_token is None or user_dict is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidSessionToken(),
            )

        # Cleaner approach with unpacking
        context_create = CreateContext(**completion_request.context.dict())
        created_context = crud.add_context(db_session, context_create)

        telemetry_create = CreateTelemetry(**completion_request.telemetry.dict())
        created_telemetry = crud.add_telemetry(db_session, telemetry_create)

        # Create query record BEFORE completions
        query_create = CreateQuery(
            user_id=uuid.UUID(user_dict["user_id"]),
            telemetry_id=created_telemetry.telemetry_id,
            context_id=created_context.context_id,
            total_serving_time=0,  # Will update this later
            server_version_id=app.get_config().server_version_id,
        )
        created_query = crud.add_query(db_session, query_create)

        # Get model completions
        start_time = datetime.now()
        completions = []

        # TODO: parallelize the completion request for different models
        for model_id in completion_request.model_ids:
            # Get model
            model = crud.get_model_by_id(db_session, model_id)
            if not model:
                continue

            # completion_text =
            completion_model = completion_models.get_model(
                model_name=model.model_name,
                prompt_template=completion.Template.PREFIX_SUFFIX,
            )
            completion_result = completion_model.invoke(
                {
                    "prefix": completion_request.context.prefix,
                    "suffix": completion_request.context.suffix,
                }
            )

            # Create generation record
            # TODO: check shown_at
            generation_create = CreateGeneration(
                query_id=created_query.query_id,
                model_id=model_id,
                completion=completion_result["completion"],
                generation_time=completion_result["generation_time"],
                shown_at=[start_time.isoformat()],
                was_accepted=False,
                confidence=completion_result["confidence"],
                logprobs=completion_result["logprobs"],
            )
            crud.add_generation(db_session, generation_create)

            # Add to response
            completions.append(
                CompletionItem(
                    model_id=model_id,
                    model_name=model.model_name,
                    completion=completion_result["completion"],
                    generation_time=completion_result["generation_time"],
                    confidence=completion_result["confidence"],
                )
            )

        # Calculate total serving time (after all models are processed)
        end_time = datetime.now()
        total_serving_time = int((end_time - start_time).total_seconds() * 1000)

        # Update query with actual total_serving_time
        crud.update_query_serving_time(
            db_session, str(created_query.query_id), total_serving_time
        )

        # Return completions (after processing all models)
        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                data=CompletionResponseData(
                    query_id=created_query.query_id, completions=completions
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error generating completions: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=ErrorResponse(message=f"Failed to generate completions: {str(e)}"),
        )
