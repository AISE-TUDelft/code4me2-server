from fastapi import APIRouter

from .chat import router as chat_router
from .completion import router as completions_router
from .multi_file_context import router as multi_file_context_router

router = APIRouter()
router.include_router(completions_router, prefix="/completion", tags=["Completion"])
router.include_router(
    multi_file_context_router,
    prefix="/completion/multi-file-context",
    tags=["Multi File Context"],
)
router.include_router(chat_router, prefix="/chat", tags=["Chat"])
