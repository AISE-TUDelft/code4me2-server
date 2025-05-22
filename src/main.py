import logging
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from App import App
from backend.routers import router
from Code4meV2Config import Code4meV2Config

load_dotenv()
# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the logging level (e.g., DEBUG, INFO, WARNING, ERROR)
    format="%(asctime)s - %(levelname)s - %(message)s",  # Define the log message format
    handlers=[logging.StreamHandler()],  # Output logs to the console
)
app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
)

config = Code4meV2Config()
if not config.test_mode:
    App().setup(config)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",
        # f"{config.website_host}:{config.website_port}"
    ],  # Allow specific origin
    allow_credentials=True,
    allow_methods=["*"],  # Allow all HTTP methods
    allow_headers=["*"],  # Allow all headers
)

# TODO: Add rate limiting middleware
# class SimpleRateLimiter(BaseHTTPMiddleware):
#     request_counts = {}
#
#     async def dispatch(self, request: Request, call_next):
#         ip = request.client.host
#         count = self.request_counts.get(ip, 0)
#         if count >= 100:
#             return JSONResponse(status_code=429, content={"detail": "Too many requests"})
#         self.request_counts[ip] = count + 1
#         return await call_next(request)
#
# app = FastAPI()
# app.add_middleware(SimpleRateLimiter)
app.include_router(router, prefix="/api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic (if any) can go here
    yield
    # Shutdown logic
    logging.warning("Shutting down the server and cleaning up resources...")
    App.get_instance().cleanup()


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=config.backend_port,
        reload=config.test_mode,
        reload_excludes=[
            "src/website/**",
        ],
    )
