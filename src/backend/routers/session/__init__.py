from fastapi import APIRouter

from .activate import router as activate_router
from .create import router as create_router
from .deactivate import router as deactivate_router

router = APIRouter()
router.include_router(create_router, prefix="/create", tags=["Create"])
router.include_router(activate_router, prefix="/activate", tags=["Activate"])
router.include_router(deactivate_router, prefix="/deactivate", tags=["Deactivate"])
