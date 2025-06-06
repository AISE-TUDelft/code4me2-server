import json
import logging

from App import App
from celery_app.celery_app import celery
from Queries import FeedbackCompletion, RequestCompletion, UpdateMultiFileContext


@celery.task
def completion_request_task(
    connection_id: str,
    session_token: str,
    completion_request: dict,
) -> None:
    """
    Process a completion request, used for background tasks.
    """

    # Import at runtime to avoid circular import
    from backend.routers.completion.request import request_completion

    logging.log(logging.INFO, f"Processing completion request: {completion_request}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()
    try:
        request_obj = RequestCompletion(**completion_request)
        response = request_completion(request_obj, app=app, session_token=session_token)
        celery_broker.publish_message(
            "completion_request_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "result": json.dumps(response.dict()),
                }
            ),
        )

    except Exception as e:
        logging.log(logging.ERROR, f"Error processing completion request: {str(e)}")
        celery_broker.publish_message(
            "completion_request_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "error": str(e),
                }
            ),
        )


@celery.task
def completion_feedback_task(
    connection_id: str, session_token: str, completion_feedback: dict
) -> None:
    """
    Process a completion request, used for background tasks.
    """
    # Import at runtime to avoid circular import
    from backend.routers.completion.feedback import submit_completion_feedback

    app = App.get_instance()
    celery_broker = app.get_celery_broker()
    try:
        response = submit_completion_feedback(
            feedback=FeedbackCompletion(**completion_feedback),
            app=app,
            session_token=session_token,
        )
        celery_broker.publish_message(
            "completion_feedback_channel",
            {
                "connection_id": connection_id,
                "result": json.dumps(response.dict()),
            },
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error processing completion feedback: {str(e)}")
        celery_broker.publish_message(
            "completion_feedback_channel",
            {
                "connection_id": connection_id,
                "result": str(e),
            },
        )


@celery.task
def update_multi_file_context_task(
    connection_id: str, session_token: str, multi_file_context_update: dict
) -> None:
    """
    Process a multi-file context update request, used for background tasks.
    """
    # Import at runtime to avoid circular import
    from backend.routers.completion.multi_file_context.update import (
        update_multi_file_context,
    )

    app = App.get_instance()
    celery_broker = app.get_celery_broker()
    try:
        response = update_multi_file_context(
            context_update=UpdateMultiFileContext(**multi_file_context_update),
            app=app,
            session_token=session_token,
        )
        celery_broker.publish_message(
            "multi_file_context_update_channel",
            {
                "connection_id": connection_id,
                "result": json.dumps(response.dict()),
            },
        )
    except Exception as e:
        logging.log(
            logging.ERROR, f"Error processing multi-file context update: {str(e)}"
        )
        celery_broker.publish_message(
            "multi_file_context_update_channel",
            {
                "connection_id": connection_id,
                "result": str(e),
            },
        )
