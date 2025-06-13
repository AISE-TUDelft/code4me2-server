"""
Celery tasks for handling LLM-related operations in Code4meV2.

This module contains asynchronous Celery tasks for processing completion requests,
feedback submissions, and multi-file context updates. All tasks use runtime imports
to avoid circular dependencies and publish results back through message channels.
"""

import json
import logging

from App import App
from celery_app.celery_app import celery
from Queries import FeedbackCompletion, RequestCompletion, UpdateMultiFileContext


@celery.task
def completion_request_task(
    connection_id: str,
    session_token: str,
    project_token: str,
    completion_request: dict,
) -> None:
    """
    Process a completion request asynchronously.

    Handles code completion requests by delegating to the appropriate router function
    and publishing the result back through the completion_request_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        completion_request: Dictionary containing the completion request parameters
    """
    # Import at runtime to avoid circular import
    from backend.routers.completion.request import request_completion

    logging.log(logging.INFO, f"Processing completion request: {completion_request}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Convert dict to RequestCompletion object and process the request
        request_obj = RequestCompletion(**completion_request)
        response = request_completion(
            request_obj,
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful result to the message channel
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

        # Publish error result to the message channel
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
    connection_id: str,
    session_token: str,
    project_token: str,
    completion_feedback: dict,
) -> None:
    """
    Process completion feedback asynchronously.

    Handles user feedback on completion results by delegating to the feedback router
    and publishing the result back through the completion_feedback_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        completion_feedback: Dictionary containing the feedback data
    """
    # Import at runtime to avoid circular import
    from backend.routers.completion.feedback import submit_completion_feedback

    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Convert dict to FeedbackCompletion object and process the feedback
        response = submit_completion_feedback(
            feedback=FeedbackCompletion(**completion_feedback),
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful result to the message channel
        celery_broker.publish_message(
            "completion_feedback_channel",
            {
                "connection_id": connection_id,
                "result": json.dumps(response.dict()),
            },
        )
    except Exception as e:
        logging.log(logging.ERROR, f"Error processing completion feedback: {str(e)}")

        # Publish error result to the message channel
        celery_broker.publish_message(
            "completion_feedback_channel",
            {
                "connection_id": connection_id,
                "result": str(e),
            },
        )


@celery.task
def update_multi_file_context_task(
    connection_id: str,
    session_token: str,
    project_token: str,
    multi_file_context_update: dict,
) -> None:
    """
    Process multi-file context update requests asynchronously.

    Handles updates to the multi-file context by delegating to the appropriate router
    and publishing the result back through the multi_file_context_update_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        multi_file_context_update: Dictionary containing the context update data
    """
    # Import at runtime to avoid circular import
    from backend.routers.completion.multi_file_context.update import (
        update_multi_file_context,
    )

    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Convert dict to UpdateMultiFileContext object and process the update
        response = update_multi_file_context(
            context_update=UpdateMultiFileContext(**multi_file_context_update),
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful result to the message channel
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

        # Publish error result to the message channel
        celery_broker.publish_message(
            "multi_file_context_update_channel",
            {
                "connection_id": connection_id,
                "result": str(e),
            },
        )
