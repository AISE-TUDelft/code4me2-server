from fastapi import FastAPI

from .routers import router

app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
)

app.include_router(router, prefix="/api")


@app.get("/")
async def read_root():
    return {"Hello": "World"}
