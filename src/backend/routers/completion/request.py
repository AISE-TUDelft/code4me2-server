import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends

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
    CompletionRequest,
    ContextCreate,
    GenerationCreate,
    QueryCreate,
    TelemetryCreate,
)

router = APIRouter()


@router.post(
    "/",
    response_model=CompletionPostResponse,
    responses={
        "200": {"model": CompletionPostResponse},
        "401": {"model": InvalidSessionToken},
        "404": {"model": ErrorResponse},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
)
def request_completion(
    completion_request: CompletionRequest,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Request code completions based on provided context.
    """
    logging.log(logging.INFO, f"Completion request: {completion_request}")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()

    try:
        # Check if user is authenticated
        user_dict = session_manager.get_session(session_token)
        if (
            session_token is None
            or user_dict is None
            or user_dict["user_id"] != completion_request.user_id
        ):
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidSessionToken(),
            )

        # Ideally the user_id will not be in session if it is deleted and therefore the following error should not be raised unless in some special concurrency cases.
        # Check if user exists
        user = crud.get_user_by_id(db_session, str(completion_request.user_id))
        if not user:
            return JsonResponseWithStatus(
                status_code=404, content=ErrorResponse(message="User not found")
            )

        # Create context using nested context data
        context_id = uuid.uuid4()
        # Cleaner approach with unpacking
        context_create = ContextCreate(
            context_id=context_id, **completion_request.context.dict()
        )
        crud.add_context(db_session, context_create)

        # Create telemetry using nested telemetry data
        telemetry_id = uuid.uuid4()
        telemetry_create = TelemetryCreate(
            telemetry_id=telemetry_id, **completion_request.telemetry.dict()
        )
        crud.add_telemetry(db_session, telemetry_create)

        # Create query FIRST - before any generations
        query_id = uuid.uuid4()
        current_time = datetime.now().isoformat()

        # Create query record BEFORE completions
        query_create = QueryCreate(
            query_id=query_id,
            user_id=completion_request.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=current_time,
            total_serving_time=0,  # Will update this later
            server_version_id=app.get_config().server_version_id,
        )
        crud.add_query(db_session, query_create)

        # Get model completions
        start_time = datetime.now()
        completions = []

        for model_id in completion_request.model_ids:
            # Get model
            model = crud.get_model_by_id(db_session, model_id)
            if not model:
                continue

            # In a real implementation, call actual model APIs
            # Here creating mock completion
            completion_text = f"def example_function():\n    # Completion from {model.model_name}\n    pass"
            generation_time = 100  # milliseconds
            confidence = 0.85
            logprobs = [-0.05, -0.1, -0.15]  # Mock logprobs

            # Create generation record
            generation_create = GenerationCreate(
                query_id=query_id,
                model_id=model_id,
                completion=completion_text,
                generation_time=generation_time,
                shown_at=[current_time],
                was_accepted=False,
                confidence=confidence,
                logprobs=logprobs,
            )
            crud.add_generation(db_session, generation_create)

            # Add to response
            completions.append(
                CompletionItem(
                    model_id=model_id,
                    model_name=model.model_name,
                    completion=completion_text,
                    confidence=confidence,
                )
            )

        # Calculate total serving time (after all models are processed)
        end_time = datetime.now()
        total_serving_time = int((end_time - start_time).total_seconds() * 1000)

        # Update query with actual total_serving_time
        if query_id is not None:
            crud.update_query_serving_time(
                db_session, str(query_id), total_serving_time
            )

        # Return completions (after processing all models)
        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionPostResponse(
                data=CompletionResponseData(query_id=query_id, completions=completions),
            ),
        )

    except Exception as e:
        logging.error(f"Error generating completions: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=ErrorResponse(message=f"Failed to generate completions: {str(e)}"),
        )
