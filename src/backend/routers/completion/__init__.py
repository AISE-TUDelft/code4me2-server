from fastapi import APIRouter

from .feedback import router as feedback_router
from .get import router as get_router
from .multi_file_context import router as multi_file_context_router
from .request import router as request_router

router = APIRouter()

# Include the router that handles completion requests, mounted under /request
router.include_router(request_router, prefix="/request", tags=["Completion"])

# Include the router that handles feedback related to completions, mounted under /feedback
router.include_router(feedback_router, prefix="/feedback", tags=["Completion"])

# Include the router that handles GET operations on completions, mounted at root
router.include_router(get_router, prefix="", tags=["Completion"])

# Include the router for multi-file context completion handling under /multi-file-context
router.include_router(
    multi_file_context_router, prefix="/multi-file-context", tags=["Multi File Context"]
)
