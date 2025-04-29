from fastapi import APIRouter

from .authenticate import router as authenticate_router
from .create import router as create_user_router
from .exists import router as user_exists_router

router = APIRouter()
router.include_router(
    authenticate_router, prefix="/authenticate", tags=["Authentication"]
)
router.include_router(user_exists_router, prefix="/exists", tags=["User Exists"])
router.include_router(create_user_router, prefix="/create", tags=["Create User"])
