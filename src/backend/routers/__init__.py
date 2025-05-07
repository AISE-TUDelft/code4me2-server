from fastapi import APIRouter

from backend.routers.user import router as user_router
from backend.routers.completion import router as completion_router

router = APIRouter()
router.include_router(user_router, prefix="/user", tags=["User"])
router.include_router(completion_router, prefix="/completion", tags=["Completion"])
