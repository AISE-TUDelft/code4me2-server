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
    session_token: str = Cookie("session_token"),
):
    await websocket.accept()
    session_manager = app.get_session_manager()
    user_data = session_manager.get_session(session_token)
    if session_token is None or user_data is None:
        logging.log(logging.INFO, f"Invalid or expired session token: {session_token}")
        await websocket.send_json(
            {"error": Responses.InvalidOrExpiredSessionToken().message}
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
                task = llm_tasks.completion_request_task.apply_async(
                    args=[connection_id, session_token, completion_request], queue="llm"
                )
                logging.log(
                    logging.INFO,
                    f"Submitted completion request with task ID: {task.id}",
                )
            elif completion_feedback:
                task = llm_tasks.completion_feedback_task.apply_async(
                    args=[connection_id, session_token, completion_feedback],
                    queue="llm",
                )
                logging.log(
                    logging.INFO, f"Submitted feedback request with task ID: {task.id}"
                )
            else:
                await websocket.send_json({"error": "Invalid websocket request"})
    except WebSocketDisconnect:
        logging.info(f"WebSocket disconnected for connection ID: {connection_id}")
    except Exception as e:
        logging.error(f"Error in WebSocket: {e}")
        await websocket.send_json({"error": str(e)})
    finally:
        broker.unregister_connection(connection_id)
