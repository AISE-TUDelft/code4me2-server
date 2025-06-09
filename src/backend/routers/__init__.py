from fastapi import APIRouter

from .chat import router as chat_router
from .completion import router as completion_router
from .project import router as project_router
from .session import router as session_router
from .user import router as user_router
from .ws import router as ws_routers

router = APIRouter()
router.include_router(session_router, prefix="/session", tags=["Session"])
router.include_router(project_router, prefix="/project", tags=["Project"])
router.include_router(user_router, prefix="/user", tags=["User"])
router.include_router(completion_router, prefix="/completion")
router.include_router(chat_router, prefix="/chat", tags=["Chat"])
router.include_router(ws_routers, prefix="/ws", tags=["WebSocket"])


@router.get("/ping")
def ping():
    return {"status": "ok"}


# for folder in filter(
#     lambda f: os.path.isdir(os.path.join(os.path.dirname(__file__), f))
#     and not f.endswith("__pycache__"),
#     os.listdir(os.path.dirname(__file__)),
# ):
#     sub_router = __import__(f"{__name__}.{folder}", fromlist=["router"]).router
#     router.include_router(sub_router, prefix=f"/{folder}", tags=folder)
