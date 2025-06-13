from fastapi import APIRouter

from .acquire import router as acquire_router
from .deactivate import router as deactivate_router

router = APIRouter()

# Include the acquire router with the /acquire prefix
router.include_router(acquire_router, prefix="/acquire")

# Include the deactivate router with the /deactivate prefix
router.include_router(deactivate_router, prefix="/deactivate")
