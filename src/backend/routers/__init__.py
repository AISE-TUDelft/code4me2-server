from fastapi import APIRouter

from .completion import router as completion_router
from .user import router as user_router

router = APIRouter()
router.include_router(user_router, prefix="/user", tags=["User"])
router.include_router(completion_router, prefix="/completion", tags=["Completion"])

# for folder in filter(
#     lambda f: os.path.isdir(os.path.join(os.path.dirname(__file__), f))
#     and not f.endswith("__pycache__"),
#     os.listdir(os.path.dirname(__file__)),
# ):
#     sub_router = __import__(f"{__name__}.{folder}", fromlist=["router"]).router
#     router.include_router(sub_router, prefix=f"/{folder}", tags=folder)
