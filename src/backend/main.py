from fastapi import FastAPI

from database import Database
from backend.utils.Code4meV2Config import Code4meV2Config
from backend.routers import router

app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
)
config = Code4meV2Config()
Database.setup(config)
app.include_router(router, prefix="/api")
