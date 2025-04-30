from fastapi import FastAPI
from .routers import auth_router
import os

from backend.database import Base
from sqlalchemy import create_engine
import backend.database.db_models


app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0"
)

# Initialize database tables
@app.on_event("startup")
async def startup():
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/coco")
    engine = create_engine(DATABASE_URL)
    Base.metadata.create_all(bind=engine)

app.include_router(
    auth_router
)

@app.get("/")
def read_root():
    return {"Hello": "World"}