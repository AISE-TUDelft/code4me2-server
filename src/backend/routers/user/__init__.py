from fastapi import APIRouter

# Import individual route modules
from .authenticate import router as authenticate_router
from .create import router as create_router
from .delete import router as delete_router
from .get import router as get_router
from .reset_password import router as reset_password_router
from .update import router as update_router
from .verify import router as verify_router

# Initialize the main API router
router = APIRouter()

# Include subrouters with specific route prefixes
router.include_router(create_router, prefix="/create")
router.include_router(update_router, prefix="/update")
router.include_router(delete_router, prefix="/delete")
router.include_router(authenticate_router, prefix="/authenticate")
router.include_router(verify_router, prefix="/verify")
router.include_router(reset_password_router, prefix="/reset-password")

router.include_router(get_router, prefix="/get")
