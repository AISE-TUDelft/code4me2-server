from fastapi import APIRouter

from backend.routers.chat import delete, get, request

router = APIRouter()
router.include_router(request.router, prefix="/request")
router.include_router(get.router, prefix="/get")
router.include_router(delete.router, prefix="/delete")
