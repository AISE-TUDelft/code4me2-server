from fastapi import APIRouter

from .completion import router as completions_router
from .multi_file_context import router as multi_file_context_router

router = APIRouter()
router.include_router(completions_router, prefix="/completion", tags=["Completion"])
router.include_router(
    multi_file_context_router, prefix="/multi-file-context", tags=["Multi File Context"]
)
