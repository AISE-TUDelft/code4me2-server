from fastapi import APIRouter

from .activate import router as activate_router
from .create import router as create_router

router = APIRouter()

# Include the 'create' sub-router with the prefix '/create'
router.include_router(create_router, prefix="/create")

# Include the 'activate' sub-router with the prefix '/activate'
router.include_router(activate_router, prefix="/activate")
