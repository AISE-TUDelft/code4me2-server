from fastapi import APIRouter

from .acquire import router as acquire_router

# from .deactivate import router as deactivate_router

router = APIRouter()
router.include_router(acquire_router, prefix="/acquire")
# router.include_router(deactivate_router, prefix="/deactivate", tags=["Deactivate"])
