import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import database.crud as crud
from App import App
from backend.models.Responses import (
    CompletionResponse,
    CompletionResponseData,
    CompletionItem,
    ErrorResponse,
    JsonResponseWithStatus,
)
from Queries import (
    CompletionRequest,
    TelemetryCreate,
    ContextCreate,
    QueryCreate,
    GenerationCreate,
)

router = APIRouter()



@router.post(
    "/",
    response_model=CompletionResponse,
    responses={
        "200": {"model": CompletionResponse},
        "404": {"model": ErrorResponse},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
)
def request_completion(
        completion_request: CompletionRequest,
        db_session: Session = Depends(App.get_db_session),
) -> JsonResponseWithStatus:
    """
    Request code completions based on provided context.
    """
    logging.log(logging.INFO, f"Completion request: {completion_request}")

    try:
        # Check if user exists
        user = crud.get_user_by_id(db_session, str(completion_request.user_id))
        if not user:
            return JsonResponseWithStatus(
                status_code=404,
                content=ErrorResponse(message="User not found")
            )

        # Create context
        context_id = uuid.uuid4()
        context_create = ContextCreate(
            context_id=context_id,
            prefix=completion_request.prefix,
            suffix=completion_request.suffix,
            language_id=completion_request.language_id,
            trigger_type_id=completion_request.trigger_type_id,
            version_id=completion_request.version_id
        )
        crud.add_context(db_session, context_create)

        # Create telemetry
        telemetry_id = uuid.uuid4()
        telemetry_create = TelemetryCreate(
            telemetry_id=telemetry_id,
            time_since_last_completion=completion_request.time_since_last_completion,
            typing_speed=completion_request.typing_speed,
            document_char_length=completion_request.document_char_length,
            relative_document_position=completion_request.relative_document_position
        )
        crud.add_telemetry(db_session, telemetry_create)

        # Create query
        query_id = uuid.uuid4()
        current_time = datetime.now().isoformat()

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
                logprobs=logprobs
            )
            crud.add_generation(db_session, generation_create)

            # Add to response
            completions.append(
                CompletionItem(
                    model_id=model_id,
                    model_name=model.model_name,
                    completion=completion_text,
                    confidence=confidence
                )
            )

        # Calculate total serving time
        end_time = datetime.now()
        total_serving_time = int((end_time - start_time).total_seconds() * 1000)

        # Create query after completions to include serving time
        query_create = QueryCreate(
            query_id=query_id,
            user_id=completion_request.user_id,
            telemetry_id=telemetry_id,
            context_id=context_id,
            timestamp=current_time,
            total_serving_time=total_serving_time,
            server_version_id=App.get_config().server_version_id
        )
        crud.add_query(db_session, query_create)

        # Return completions
        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionResponse(
                message="Completions generated successfully",
                data=CompletionResponseData(
                    query_id=query_id,
                    completions=completions
                )
            )
        )

    except Exception as e:
        logging.error(f"Error generating completions: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=ErrorResponse(message=f"Failed to generate completions: {str(e)}")
        )