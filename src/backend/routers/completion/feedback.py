import logging
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
from App import App
from backend.Responses import (
    CompletionFeedbackPostResponse,
    ErrorResponse,
    InvalidSessionToken,
    JsonResponseWithStatus,
    GenerationNotFoundError,
    FeedbackRecordingError,
)
from base_models import FeedbackResponseData
from Queries import CreateGroundTruth, FeedbackCompletion

router = APIRouter()


@router.post(
    "/",
    response_model=CompletionFeedbackPostResponse,
    responses={
        "200": {"model": CompletionFeedbackPostResponse},
        "401": {"model": InvalidSessionToken},
        "404": {"model": GenerationNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": FeedbackRecordingError},
    },
)
def submit_completion_feedback(
    feedback: FeedbackCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Submit feedback on a generated completion.
    """
    logging.log(logging.INFO, f"Completion feedback: {feedback}")
    db_session = app.get_db_session()
    session_manager = app.get_session_manager()
    try:
        user_dict = session_manager.get_session(session_token)
        if session_token is None or user_dict is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidSessionToken(),
            )
        # Get the generation record
        generation = crud.get_generations_by_query_and_model_id(
            db_session, str(feedback.query_id), feedback.model_id
        )

        if not generation:
            return JsonResponseWithStatus(
                status_code=404,
                content=GenerationNotFoundError(),
            )

        # Update generation status
        crud.update_generation_acceptance(
            db_session, str(feedback.query_id), feedback.model_id, feedback.was_accepted
        )

        # If ground truth is provided, save it
        if feedback.ground_truth:
            ground_truth_create = CreateGroundTruth(
                query_id=feedback.query_id,
                truth_timestamp=datetime.now().isoformat(),
                ground_truth=feedback.ground_truth,
            )
            crud.add_ground_truth(db_session, ground_truth_create)

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionFeedbackPostResponse(
                data=FeedbackResponseData(
                    query_id=feedback.query_id, model_id=feedback.model_id
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error recording feedback: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=FeedbackRecordingError(str(e)),
        )
