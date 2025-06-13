from fastapi import APIRouter

from .update import router as update_router

# Create a new FastAPI router for this module
router = APIRouter()

# Include the 'update' router under the '/update' prefix
router.include_router(update_router, prefix="/update")
