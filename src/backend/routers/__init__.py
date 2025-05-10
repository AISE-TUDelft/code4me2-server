import os

from fastapi import APIRouter

from backend.routers.user import router as user_router

router = APIRouter()

for folder in filter(
    lambda f: os.path.isdir(os.path.join(os.path.dirname(__file__), f))
    and not f.endswith("__pycache__"),
    os.listdir(os.path.dirname(__file__)),
):
    sub_router = __import__(f"{__name__}.{folder}", fromlist=["router"]).router
    router.include_router(sub_router, prefix=f"/{folder}", tags=folder)
