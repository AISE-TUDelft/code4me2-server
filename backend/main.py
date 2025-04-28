from fastapi import FastAPI
from .routers import auth_router

app = FastAPI(
    title="Code4Me V2 API",
    description="The complte API for Code4Me V2",
    version="1.0.0",
)

app.include_router(auth_router)


@app.get("/")
def read_root():
    return {"Hello": "World"}
