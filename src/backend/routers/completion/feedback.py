import logging
from typing import Union

from fastapi import APIRouter, Cookie, Depends

import database.crud as crud
import Queries
from App import App
from backend.Responses import (
    CompletionFeedbackPostResponse,
    ErrorResponse,
    FeedbackRecordingError,
    GenerationNotFoundError,
    InvalidOrExpiredAuthToken,
    InvalidOrExpiredProjectToken,
    InvalidOrExpiredSessionToken,
    JsonResponseWithStatus,
    NoAccessToProvideFeedbackError,
)
from celery_app.tasks import db_tasks
from response_models import ResponseFeedbackResponseData

router = APIRouter()


@router.post(
    "",
    response_model=CompletionFeedbackPostResponse,
    responses={
        "200": {"model": CompletionFeedbackPostResponse},
        "401": {
            "model": Union[
                InvalidOrExpiredSessionToken,
                InvalidOrExpiredAuthToken,
                InvalidOrExpiredProjectToken,
            ]
        },
        "403": {"model": NoAccessToProvideFeedbackError},
        "404": {"model": GenerationNotFoundError},
        "422": {"model": ErrorResponse},
        "429": {"model": ErrorResponse},
        "500": {"model": FeedbackRecordingError},
    },
)
def submit_completion_feedback(
    feedback: Queries.FeedbackCompletion,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
) -> JsonResponseWithStatus:
    """
    Submit feedback on a generated completion.

    This endpoint validates the user's session and project tokens against Redis,
    verifies the user's authorization to provide feedback on the given query,
    and enqueues asynchronous tasks to update generation status and optionally
    save ground truth feedback.

    Parameters:
    - feedback: The feedback data submitted by the user, including acceptance
      status and optional ground truth.
    - app: Dependency-injected application instance providing DB and Redis access.
    - session_token: Session cookie used to authenticate the user session.
    - project_token: Project cookie used to authorize project-level access.

    Returns:
    - JsonResponseWithStatus: A response indicating success or describing errors,
      including authorization failures, missing records, or internal errors.
    """
    logging.info(f"Completion feedback: {feedback}")

    db_session = app.get_db_session()
    redis_manager = app.get_redis_manager()

    try:
        # Validate the session token and retrieve session info from Redis
        session_info = redis_manager.get("session_token", session_token)
        if session_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Extract and validate auth token from session info
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
                content=InvalidOrExpiredAuthToken(),
            )
        user_id = auth_info.get("user_id")
        if not user_id:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredSessionToken(),
            )

        # Validate the project token and confirm it is linked to the session
        project_info = redis_manager.get("project_token", project_token)
        if project_info is None:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredProjectToken(),
            )
        session_projects = session_info.get("project_tokens", [])
        if project_token not in session_projects:
            return JsonResponseWithStatus(
                status_code=401,
                content=InvalidOrExpiredProjectToken(),
            )

        # Retrieve the meta query and verify ownership by the current user
        query = crud.get_meta_query_by_id(db_session, feedback.meta_query_id)
        if not query:
            return JsonResponseWithStatus(
                status_code=404,
                content=GenerationNotFoundError(),
            )

        # Compare user IDs to enforce access control
        if str(query.user_id) != user_id:
            return JsonResponseWithStatus(
                status_code=403,
                content=NoAccessToProvideFeedbackError(),
            )

        # Retrieve the generation record for the specified query and model
        generation = crud.get_generation_by_meta_query_and_model(
            db_session, feedback.meta_query_id, feedback.model_id
        )
        if not generation:
            return JsonResponseWithStatus(
                status_code=404,
                content=GenerationNotFoundError(),
            )

        # Enqueue task to update the generation status asynchronously
        db_tasks.update_generation_task.apply_async(
            args=[
                str(feedback.meta_query_id),
                feedback.model_id,
                Queries.UpdateGeneration(was_accepted=feedback.was_accepted).dict(),
            ],
            queue="db",
        )

        # If ground truth is provided, enqueue task to add it asynchronously
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
                    meta_query_id=feedback.meta_query_id,
                    model_id=feedback.model_id,
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
