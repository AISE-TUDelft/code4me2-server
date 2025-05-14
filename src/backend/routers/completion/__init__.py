from fastapi import APIRouter

from .feedback import router as feedback_router
from .get import router as get_router
from .request import router as request_router

router = APIRouter()
router.include_router(request_router, prefix="/request", tags=["Request Completion"])
router.include_router(feedback_router, prefix="/feedback", tags=["Completion Feedback"])
router.include_router(get_router, prefix="", tags=["Get Completions"])
