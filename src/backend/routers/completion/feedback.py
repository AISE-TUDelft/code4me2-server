import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import database.crud as crud
from App import App
from backend.models.Responses import (
    FeedbackResponse,
    FeedbackResponseData,
    ErrorResponse,
    JsonResponseWithStatus,
)
from Queries import CompletionFeedback, GroundTruthCreate

router = APIRouter()


@router.post(
    "/",
    response_model=FeedbackResponse,
    responses={
        "200": {"model": FeedbackResponse},
        "404": {"model": ErrorResponse},
        "422": {"model": ErrorResponse},
        "500": {"model": ErrorResponse},
    },
)
def submit_completion_feedback(
    feedback: CompletionFeedback,
    db_session: Session = Depends(App.get_db_session),
) -> JsonResponseWithStatus:
    """
    Submit feedback on a generated completion.
    """
    logging.log(logging.INFO, f"Completion feedback: {feedback}")

    try:
        # Get the generation record
        generation = crud.get_generations_by_query_and_model_id(
            db_session, str(feedback.query_id), feedback.model_id
        )

        if not generation:
            return JsonResponseWithStatus(
                status_code=404,
                content=ErrorResponse(message="Generation record not found"),
            )

        # Update generation status
        crud.update_generation_acceptance(
            db_session, str(feedback.query_id), feedback.model_id, feedback.was_accepted
        )

        # If ground truth is provided, save it
        if feedback.ground_truth:
            ground_truth_create = GroundTruthCreate(
                query_id=feedback.query_id,
                truth_timestamp=datetime.now().isoformat(),
                ground_truth=feedback.ground_truth,
            )
            crud.add_ground_truth(db_session, ground_truth_create)

        return JsonResponseWithStatus(
            status_code=200,
            content=FeedbackResponse(
                message="Feedback recorded successfully",
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
            content=ErrorResponse(message=f"Failed to record feedback: {str(e)}"),
        )
