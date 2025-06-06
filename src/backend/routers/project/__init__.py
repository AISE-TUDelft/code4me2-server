from fastapi import APIRouter

from .activate import router as activate_router
from .create import router as create_router

router = APIRouter()
router.include_router(create_router, prefix="/create")
router.include_router(activate_router, prefix="/activate")
