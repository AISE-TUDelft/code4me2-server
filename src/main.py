import logging
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from App import App
from backend.Responses import JsonResponseWithStatus, TooManyRequests
from backend.routers import router
from Code4meV2Config import Code4meV2Config

# Configure logging
logging.basicConfig(
    level=logging.INFO,  # Set the logging level (e.g., DEBUG, INFO, WARNING, ERROR)
    format="%(asctime)s - %(levelname)s - %(message)s",  # Define the log message format
    handlers=[logging.StreamHandler()],  # Output logs to the console
    force=True,
)

load_dotenv()
config = Code4meV2Config()  # type: ignore


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.test_mode:
        logging.log(logging.INFO, "Starting the server and initializing resources...")
        _app = App()
        yield
        logging.warning("Shutting down the server and cleaning up resources...")
        _app.cleanup()
    else:
        yield


class SimpleRateLimiter(BaseHTTPMiddleware):
    request_counts = {}
    locks = {}

    def __init__(self, app):
        super().__init__(app)
        threading.Thread(target=self.reset_counts_periodically, daemon=True).start()

    def reset_counts_periodically(self):
        while True:
            time.sleep(3600)  # Wait for 1 hour
            self.request_counts.clear()

    async def dispatch(self, request: Request, call_next):
        ip = request.client.host
        endpoint = request.url.path
        key = f"{ip}:{endpoint}"  # Combine IP and endpoint for unique tracking
        if key not in self.locks:
            self.locks[key] = threading.Lock()
        with self.locks[key]:
            count = self.request_counts.get(key, 0)
            if count >= config.max_request_rate_per_hour_config.get(
                endpoint, config.default_max_request_rate_per_hour
            ):
                return JsonResponseWithStatus(
                    status_code=429, content=TooManyRequests()
                )
            self.request_counts[key] = count + 1
        return await call_next(request)


app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
    lifespan=lifespan,
)
# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
if not config.test_mode:
    app.add_middleware(SimpleRateLimiter)
app.include_router(router, prefix="/api")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.server_host,
        port=config.server_port,
    )
