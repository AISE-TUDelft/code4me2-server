import logging

from fastapi import APIRouter, Cookie, Depends, WebSocket, WebSocketDisconnect

import celery_app.tasks.llm_tasks as llm_tasks
from App import App
from backend import Responses

router = APIRouter()


@router.websocket("")
async def completions_websocket(
    websocket: WebSocket,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie(""),
    project_token: str = Cookie(""),
):
    """
    WebSocket endpoint to handle code completion and feedback requests.

    Validates session, auth, and project tokens from cookies and Redis.
    Registers the WebSocket connection with the Celery broker and
    listens for either completion requests or feedback, which are
    dispatched as Celery tasks.

    Args:
        websocket (WebSocket): The WebSocket connection.
        app (App): Application instance, injected via FastAPI dependency.
        session_token (str): Session token cookie.
        project_token (str): Project token cookie.
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

    # Get auth_token from session info and validate
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

    # Extract user_id and verify
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

    # Verify that the project token is linked to this session
    session_projects = session_info.get("project_tokens", [])
    if project_token not in session_projects:
        logging.info(f"Invalid or expired project token: {project_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredProjectToken().message}
        )
        await websocket.close()
        return

    broker = app.get_celery_broker()
    connection_id = broker.register_new_connection(websocket)

    try:
        while True:
            data = await websocket.receive_json()
            completion_request = data.get("completion_request")
            completion_feedback = data.get("completion_feedback")

            if completion_request:
                # Dispatch the completion request task to Celery
                task = llm_tasks.completion_request_task.apply_async(
                    args=[
                        connection_id,
                        session_token,
                        project_token,
                        completion_request,
                    ],
                    queue="llm",
                )
                logging.info(f"Submitted completion request with task ID: {task.id}")

            elif completion_feedback:
                # Dispatch the completion feedback task to Celery
                task = llm_tasks.completion_feedback_task.apply_async(
                    args=[
                        connection_id,
                        session_token,
                        project_token,
                        completion_feedback,
                    ],
                    queue="llm",
                )
                logging.info(f"Submitted feedback request with task ID: {task.id}")

            else:
                # Invalid payload received
                await websocket.send_json({"error": "Invalid websocket request"})

    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for connection ID: {connection_id}")
    except Exception as e:
        logging.error(f"Error in WebSocket: {e}")
        await websocket.send_json({"error": str(e)})
    finally:
        broker.unregister_connection(connection_id)
