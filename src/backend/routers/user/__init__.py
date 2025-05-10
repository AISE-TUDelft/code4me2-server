import os

from fastapi import APIRouter

router = APIRouter()

for filename in os.listdir(os.path.dirname(__file__)):
    if filename.endswith(".py") and filename != "__init__.py":
        module_name = filename[:-3]
        sub_router = __import__(f"{__name__}.{module_name}", fromlist=["router"]).router
        router.include_router(
            sub_router,
            prefix=f"/{module_name}",
            tags=[module_name.replace("_", " ").title() + __name__.title()],
        )
