from fastapi import FastAPI

from src.backend.utils.Config import Config
from .routers import router

app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
)
# app.config = Config()

app.include_router(router, prefix="/api")
