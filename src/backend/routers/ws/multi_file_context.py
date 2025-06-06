import logging

from fastapi import APIRouter, Cookie, Depends, WebSocket, WebSocketDisconnect

import celery_app.tasks.llm_tasks as llm_tasks
from App import App
from backend import Responses

router = APIRouter()


@router.websocket("")
async def multi_file_context_websocket(
    websocket: WebSocket,
    app: App = Depends(App.get_instance),
    session_token: str = Cookie("session_token"),
):
    await websocket.accept()
    redis_manager = app.get_redis_manager()
    user_data = redis_manager.get_session(session_token)
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
            multi_file_context_update = data.get("multi_file_context_update")
            if multi_file_context_update:
                task = llm_tasks.update_multi_file_context_task.apply_async(
                    args=[connection_id, session_token, multi_file_context_update],
                    queue="llm",
                )
                logging.log(
                    logging.INFO,
                    f"Submitted multi-file context update with task ID: {task.id}",
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
