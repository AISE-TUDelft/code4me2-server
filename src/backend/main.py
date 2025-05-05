from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from App import App
from Code4meV2Config import Code4meV2Config
from backend.routers import router

app = FastAPI(
    title="Code4Me V2 API",
    description="The complete API for Code4Me V2",
    version="1.0.0",
)

config = Code4meV2Config()
App.setup(config)

# Configure CORS
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=[config.react_app_url],  # Allow specific origin
#     allow_credentials=True,
#     allow_methods=["*"],  # Allow all HTTP methods
#     allow_headers=["*"],  # Allow all headers
# )

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
