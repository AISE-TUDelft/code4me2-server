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
    Process a chat completion request, used for background tasks.
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.request import request_chat_completion

    logging.log(logging.INFO, f"Processing chat completion request: {chat_request}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        request_obj = Queries.RequestChatCompletion(**chat_request)
        response = request_chat_completion(
            chat_completion_request=request_obj,
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

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
    Process a chat history request, used for background tasks.
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.get import get_chat_history

    logging.log(logging.INFO, f"Processing chat history request: {chat_get}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        page_number = chat_get.get("page_number", 1)
        response = get_chat_history(
            app=app,
            page_number=page_number,
            session_token=session_token,
            project_token=project_token,
        )

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
    Process a chat deletion request, used for background tasks.
    """
    # Import at runtime to avoid circular import
    from backend.routers.chat.delete import delete_chat

    logging.log(logging.INFO, f"Processing chat deletion request: {chat_delete}")
    app = App.get_instance()
    celery_broker = app.get_celery_broker()

    try:
        from uuid import UUID

        chat_id = UUID(chat_delete.get("chat_id"))
        response = delete_chat(
            chat_id=chat_id,
            app=app,
            session_token=session_token,
            project_token=project_token,
        )

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
        celery_broker.publish_message(
            "chat_delete_channel",
            json.dumps(
                {
                    "connection_id": connection_id,
                    "error": str(e),
                }
            ),
        )
