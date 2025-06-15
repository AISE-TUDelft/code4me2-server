"""
Celery tasks for chat-related operations in Code4meV2.

This module contains asynchronous Celery tasks for handling chat completion requests,
retrieving chat history, and deleting chat sessions. All tasks follow the same pattern
of runtime imports to avoid circular dependencies and publish results through dedicated
message channels for real-time client communication.
"""

import json
import logging

import Queries
from App import App
from celery_app.celery_app import celery


@celery.task
def chat_request_task(
    connection_id: str,
    session_token: str,
    project_token: str,
    chat_request: dict,
) -> None:
    """
    Process a chat completion request asynchronously.

    Handles conversational AI requests by delegating to the chat completion router
    and publishing the generated response back through the chat_request_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        chat_request: Dictionary containing the chat completion request parameters
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.request import request_chat_completion

    logging.log(logging.INFO, f"Processing chat completion request: {chat_request}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Convert dict to RequestChatCompletion object and process the request
        request_obj = Queries.RequestChatCompletion(**chat_request)
        response = request_chat_completion(
            chat_completion_request=request_obj,
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful response to the message channel
        celery_broker.publish_message(
            "chat_request_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "result": json.dumps(response.dict()),
                }
            ),
        )

    except Exception as e:
        logging.log(
            logging.ERROR, f"Error processing chat completion request: {str(e)}"
        )

        # Publish error result to the message channel
        celery_broker.publish_message(
            "chat_request_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "error": str(e),
                }
            ),
        )


@celery.task
def chat_get_task(
    connection_id: str,
    session_token: str,
    project_token: str,
    chat_get: dict,
) -> None:
    """
    Process a chat history retrieval request asynchronously.

    Retrieves paginated chat history for a user session by delegating to the
    chat history router and publishing the results through the chat_get_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        chat_get: Dictionary containing pagination parameters (e.g., page_number)
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.get import get_chat_history

    logging.log(logging.INFO, f"Processing chat history request: {chat_get}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Extract page number from request, defaulting to first page
        page_number = chat_get.get("page_number", 1)
        response = get_chat_history(
            app=app,
            page_number=page_number,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful response to the message channel
        celery_broker.publish_message(
            "chat_get_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "result": json.dumps(response.dict()),
                }
            ),
        )

    except Exception as e:
        logging.log(logging.ERROR, f"Error processing chat history request: {str(e)}")

        # Publish error result to the message channel
        celery_broker.publish_message(
            "chat_get_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "error": str(e),
                }
            ),
        )


@celery.task
def chat_delete_task(
    connection_id: str,
    session_token: str,
    project_token: str,
    chat_delete: dict,
) -> None:
    """
    Process a chat deletion request asynchronously.

    Handles chat session deletion by converting the chat ID to UUID format,
    delegating to the chat deletion router, and publishing confirmation
    through the chat_delete_channel.

    Args:
        connection_id: Unique identifier for the client connection
        session_token: Authentication token for the user session
        project_token: Token identifying the specific project context
        chat_delete: Dictionary containing the chat_id to be deleted
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.delete import delete_chat

    logging.log(logging.INFO, f"Processing chat deletion request: {chat_delete}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        # Import UUID locally to convert string ID to UUID object
        from uuid import UUID

        # Extract and convert chat_id from string to UUID format
        chat_id = UUID(chat_delete.get("chat_id"))
        response = delete_chat(
            chat_id=chat_id,
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

        # Publish successful deletion confirmation to the message channel
        celery_broker.publish_message(
            "chat_delete_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "result": json.dumps(response.dict()),
                }
            ),
        )

    except Exception as e:
        logging.log(logging.ERROR, f"Error processing chat deletion request: {str(e)}")

        # Publish error result to the message channel
        celery_broker.publish_message(
            "chat_delete_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "error": str(e),
                }
            ),
        )
