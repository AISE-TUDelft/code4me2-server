import logging

from fastapi import APIRouter, Cookie, Depends, WebSocket, WebSocketDisconnect

import celery_app.tasks.chat_tasks as chat_tasks
from App import App
from backend import Responses

router = APIRouter()


@router.websocket("")
async def chat_websocket(
    websocket: WebSocket,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
):
    """
    WebSocket endpoint to handle chat-related operations.

    Validates session, auth, and project tokens. Once authenticated,
    listens for messages including chat requests, chat history retrieval,
    or chat deletion, and dispatches corresponding Celery tasks.

    Args:
        websocket (WebSocket): WebSocket connection from the client.
        app (App): Application instance injected by FastAPI.
        session_token (str): Session token for authentication.
        project_token (str): Project token for access control.
    """
    await websocket.accept()
    redis_manager = app.get_redis_manager()

    # Validate session token
    session_info = redis_manager.get("session_token", session_token)
    if session_info is None:
        logging.info(f"Invalid or expired session token: {session_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredSessionToken().message}
        )
        await websocket.close()
        return

    # Extract and validate auth token
    auth_token = session_info.get("auth_token")
    if not auth_token:
        logging.info(f"Invalid or expired session token: {session_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredSessionToken().message}
        )
        await websocket.close()
        return

    auth_info = redis_manager.get("auth_token", auth_token)
    if auth_info is None:
        logging.info(f"Invalid or expired auth token: {auth_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredAuthToken().message}
        )
        await websocket.close()
        return

    # Validate user ID from auth token
    user_id = auth_info.get("user_id")
    if not user_id:
        logging.info(f"Invalid or expired session token: {session_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredSessionToken().message}
        )
        await websocket.close()
        return

    # Validate project token
    project_info = redis_manager.get("project_token", project_token)
    if project_info is None:
        logging.info(f"Invalid or expired project token: {project_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredProjectToken().message}
        )
        await websocket.close()
        return

    # Ensure project is associated with the session
    session_projects = session_info.get("project_tokens", [])
    if project_token not in session_projects:
        logging.info(f"Invalid or expired project token: {project_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredProjectToken().message}
        )
        await websocket.close()
        return

    # Register the WebSocket connection for task communication
    broker = app.get_celery_broker()
    connection_id = broker.register_new_connection(websocket)

    try:
        while True:
            data = await websocket.receive_json()

            chat_request = data.get("chat_request")
            chat_get = data.get("chat_get")
            chat_delete = data.get("chat_delete")

            if chat_request:
                # Submit task to process new chat messages
                task = chat_tasks.chat_request_task.apply_async(
                    args=[connection_id, session_token, project_token, chat_request],
                    queue="llm",
                )
                logging.info(
                    f"Submitted chat completion request with task ID: {task.id}"
                )

            elif chat_get:
                # Submit task to retrieve chat history
                task = chat_tasks.chat_get_task.apply_async(
                    args=[connection_id, session_token, project_token, chat_get],
                    queue="db",
                )
                logging.info(f"Submitted chat history request with task ID: {task.id}")

            elif chat_delete:
                # Submit task to delete chat history
                task = chat_tasks.chat_delete_task.apply_async(
                    args=[connection_id, session_token, project_token, chat_delete],
                    queue="db",
                )
                logging.info(f"Submitted chat deletion request with task ID: {task.id}")

            else:
                # Received payload is invalid
                await websocket.send_json({"error": "Invalid websocket request"})

    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for connection ID: {connection_id}")
    except Exception as e:
        logging.error(f"Error in WebSocket: {e}")
        await websocket.send_json({"error": str(e)})
    finally:
        # Clean up connection reference
        broker.unregister_connection(connection_id)
