import logging
from uuid import UUID

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries
from App import App
from backend.Responses import (
    CompletionFeedbackPostResponse,
    ErrorResponse,
    FeedbackRecordingError,
    GenerationNotFoundError,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
)
from celery_app.tasks import db_tasks
from response_models import ResponseFeedbackResponseData

router = APIRouter()


@router.post(
    "/",
    response_model=CompletionFeedbackPostResponse,
    responses={
        "200": {"model": CompletionFeedbackPostResponse},
        "401": {"model": InvalidOrExpiredSessionToken},
        "404": {"model": GenerationNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": FeedbackRecordingError},
    },
)
def submit_completion_feedback(
    feedback: Queries.FeedbackCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
) -> JsonResponseWithStatus:
    """
    Submit feedback on a generated completion.
    """
    logging.info(f"Completion feedback: {feedback}")
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

        # only allow feedback for queries that are for the current user
        query = crud.get_meta_query_by_id(db_session, str(feedback.meta_query_id))
        if not query:
            return JsonResponseWithStatus(
                status_code=404,
                content=GenerationNotFoundError(),
            )

        # Convert user_id from string (Redis) to UUID for comparison with database user_id (UUID)
        if query.user_id != UUID(user_id):
            return JsonResponseWithStatus(
                status_code=403,
                content=ErrorResponse(
                    message="You are not allowed to provide feedback for this query."
                ),
            )

        # Get the generation record
        generation = crud.get_generation_by_meta_query_and_model(
            db_session, str(feedback.meta_query_id), feedback.model_id
        )

        if not generation:
            return JsonResponseWithStatus(
                status_code=404,
                content=GenerationNotFoundError(),
            )

        # Update generation status
        db_tasks.update_generation_task.apply_async(
            args=[
                str(feedback.meta_query_id),
                feedback.model_id,
                Queries.UpdateGeneration(was_accepted=feedback.was_accepted).dict(),
            ],
            queue="db",
        )

        # If ground truth is provided, save it
        if feedback.ground_truth:
            db_tasks.add_ground_truth_task.apply_async(
                args=[
                    Queries.CreateGroundTruth(
                        completion_query_id=feedback.meta_query_id,
                        ground_truth=feedback.ground_truth,
                    ).dict()
                ],
                queue="db",
            )

        return JsonResponseWithStatus(
            status_code=200,
            content=CompletionFeedbackPostResponse(
                data=ResponseFeedbackResponseData(
                    meta_query_id=feedback.meta_query_id, model_id=feedback.model_id
                ),
            ),
        )

    except Exception as e:
        logging.error(f"Error recording feedback: {str(e)}")
        db_session.rollback()
        return JsonResponseWithStatus(
            status_code=500,
            content=FeedbackRecordingError(),
        )
