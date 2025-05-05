from fastapi import APIRouter

from .authenticate import router as authenticate_router
from .create import router as create_user_router

router = APIRouter()
router.include_router(
    authenticate_router, prefix="/authenticate", tags=["Authenticate User"]
)
router.include_router(create_user_router, prefix="/create", tags=["Create User"])
