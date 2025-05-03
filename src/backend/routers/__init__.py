from fastapi import APIRouter, Depends

from backend.routers.user import router as user_router
from database import Database

router = APIRouter()
router.include_router(
    user_router,
    prefix="/user",
    tags=["User"],
    dependencies=[Depends(Database.get_db_session)],
)
