from fastapi import APIRouter

from .update import router as update_router

router = APIRouter()
router.include_router(
    update_router, prefix="/update", tags=["Update Multi File Context"]
)
