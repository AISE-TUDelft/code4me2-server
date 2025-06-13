from fastapi import APIRouter

from .chat import router as chat_router
from .completion import router as completions_router
from .multi_file_context import router as multi_file_context_router

# Root API router that includes sub-routers for modular endpoints
router = APIRouter()

# Include the completion endpoint routes
router.include_router(completions_router, prefix="/completion", tags=["Completion"])

# Include multi-file context routes under the completion namespace
router.include_router(
    multi_file_context_router,
    prefix="/completion/multi-file-context",
    tags=["Multi File Context"],
)

# Include chat-related WebSocket and HTTP routes
router.include_router(chat_router, prefix="/chat", tags=["Chat"])
