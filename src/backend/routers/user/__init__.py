from fastapi import APIRouter

from .authenticate import router as authenticate_router
from .create import router as create_router
from .delete import router as delete_router
from .update import router as update_router
from .verify import router as verify_router

router = APIRouter()
router.include_router(create_router, prefix="/create")
router.include_router(update_router, prefix="/update")
router.include_router(delete_router, prefix="/delete")
router.include_router(authenticate_router, prefix="/authenticate")
router.include_router(verify_router, prefix="/verify")

# for filename in os.listdir(os.path.dirname(__file__)):
#     if filename.endswith(".py") and filename != "__init__.py":
#         module_name = filename[:-3]
#         sub_router = __import__(f"{__name__}.{module_name}", fromlist=["router"]).router
#         router.include_router(
#             sub_router,
#             prefix=f"/{module_name}",
#             tags=[module_name.replace("_", " ").title() + __name__.title()],
#         )
