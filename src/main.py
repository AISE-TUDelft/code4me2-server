#!/usr/bin/env python3
"""
FastAPI Application Entry Point for Code4Me V2 API

This module sets up a FastAPI server with CORS middleware, rate limiting,
logging configuration, and lifecycle management for the Code4Me V2 application.

Usage:
    python main.py

Environment:
    Requires .env file with configuration variables.
    See Code4meV2Config for required environment variables.

Author: Your Name
Version: 1.0.0
"""

import logging
import threading
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from App import App
from backend.Responses import JsonResponseWithStatus, TooManyRequests
from backend.routers import router
from Code4meV2Config import Code4meV2Config

# Global configuration
load_dotenv()
config = Code4meV2Config()


def configure_logging() -> None:
    """
    Configure logging for the application.

    Sets up logging with INFO level, timestamp format, and console output.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI lifespan context manager for startup and shutdown events.

    Args:
        app: The FastAPI application instance

    Yields:
        None: Control back to the application during its lifetime

    Note:
        Only initializes resources when not in test mode.
    """
    if not config.test_mode:
        logging.info("Starting the server and initializing resources...")
        _app = App()
        try:
            yield
        finally:
            logging.warning("Shutting down the server and cleaning up resources...")
            _app.cleanup()
    else:
        logging.info("Running in test mode - skipping resource initialization")
        yield


class SimpleRateLimiter(BaseHTTPMiddleware):
    """
    Simple rate limiting middleware for FastAPI.

    Implements per-IP, per-endpoint rate limiting with configurable limits.
    Request counts are reset every hour automatically.

    Attributes:
        request_counts: Dictionary tracking request counts per IP-endpoint combination
        locks: Threading locks for thread-safe request counting
    """

    def __init__(self, app: FastAPI) -> None:
        """
        Initialize the rate limiter middleware.

        Args:
            app: The FastAPI application instance
        """
        super().__init__(app)
        self.request_counts: Dict[str, int] = {}
        self.locks: Dict[str, threading.Lock] = {}
        self._start_reset_thread()

    def _start_reset_thread(self) -> None:
        """Start the background thread that resets request counts periodically."""
        reset_thread = threading.Thread(
            target=self._reset_counts_periodically,
            daemon=True,
            name="RateLimiterResetThread",
        )
        reset_thread.start()
        logging.info("Rate limiter reset thread started")

    def _reset_counts_periodically(self) -> None:
        """
        Background thread function that resets request counts every hour.

        Runs indefinitely, clearing all request counts every 3600 seconds (1 hour).
        """
        while True:
            time.sleep(3600)  # Wait for 1 hour
            with threading.Lock():
                count_before = len(self.request_counts)
                self.request_counts.clear()
                logging.info(f"Reset {count_before} rate limit counters")

    def _get_rate_limit(self, endpoint: str) -> int:
        """
        Get the rate limit for a specific endpoint.

        Args:
            endpoint: The API endpoint path

        Returns:
            The maximum number of requests allowed per hour for this endpoint
        """
        return config.max_request_rate_per_hour_config.get(
            endpoint, config.default_max_request_rate_per_hour
        )

    def _get_client_key(self, request: Request) -> str:
        """
        Generate a unique key for tracking client requests.

        Args:
            request: The incoming HTTP request

        Returns:
            A unique string combining client IP and endpoint path
        """
        ip = request.client.host if request.client else "unknown"
        endpoint = request.url.path
        return f"{ip}:{endpoint}"

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Process incoming requests and apply rate limiting.

        Args:
            request: The incoming HTTP request
            call_next: The next middleware or route handler

        Returns:
            HTTP response, either rate limit error or normal response
        """
        client_key = self._get_client_key(request)
        endpoint = request.url.path

        # Ensure thread-safe access to request counts
        if client_key not in self.locks:
            self.locks[client_key] = threading.Lock()

        with self.locks[client_key]:
            current_count = self.request_counts.get(client_key, 0)
            rate_limit = self._get_rate_limit(endpoint)
            logging.info(
                f"Request sent to {endpoint} from {request.client.host if request.client else 'unknown'}."
                f"\nRate limit: {current_count}/{rate_limit}\tCookies: {request.cookies}\tBody: {await request.body()}"
            )
            if current_count >= rate_limit:
                logging.warning(
                    f"Rate limit exceeded for {client_key} "
                    f"({current_count}/{rate_limit})"
                )
                return JsonResponseWithStatus(
                    status_code=429, content=TooManyRequests()
                )

            self.request_counts[client_key] = current_count + 1

        return await call_next(request)


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application instance.

    Returns:
        Configured FastAPI application with middleware and routes
    """
    # Configure logging first
    configure_logging()

    # Create FastAPI app with metadata
    app = FastAPI(
        title="Code4Me V2 API",
        description="The complete API for Code4Me V2",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if not config.test_mode else None,
        redoc_url="/redoc" if not config.test_mode else None,
    )

    # Configure CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Consider restricting in production
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    # Add rate limiting middleware (skip in test mode)
    if not config.test_mode:
        app.add_middleware(SimpleRateLimiter)
        logging.info("Rate limiting middleware enabled")
    else:
        logging.info("Rate limiting middleware disabled (test mode)")

    # Include API routes
    app.include_router(router, prefix="/api")

    logging.info("FastAPI application created and configured")
    return app


# Create the app instance
app = create_app()


def main() -> None:
    """
    Main entry point for running the FastAPI server.

    Uses uvicorn to serve the application with configuration from Code4meV2Config.
    """
    logging.info(
        f"Starting Code4Me V2 API server on "
        f"{config.server_host}:{config.server_port}"
    )

    uvicorn.run(
        "main:app",
        host=config.server_host,
        port=config.server_port,
        reload=config.debug_mode if hasattr(config, "debug_mode") else False,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
