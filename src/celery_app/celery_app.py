"""
Celery application configuration for Code4meV2.

This module initializes and configures a Celery app for distributed task processing,
with Redis as both message broker and result backend. It sets up multiprocessing
compatibility for CUDA environments and defines task routing to separate queues.
"""

import logging

# Set multiprocessing start method early for CUDA compatibility
# This must be done before importing torch to avoid CUDA initialization issues
import multiprocessing

from Code4meV2Config import Code4meV2Config

multiprocessing.set_start_method("spawn", force=True)

import torch  # noqa: E402

# Ensure torch also uses spawn method for multiprocessing compatibility
torch.multiprocessing.set_start_method("spawn", force=True)

from celery import Celery  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import celery_app.worker_init  # noqa

# Configure logging for the application
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
)

# Load environment variables and configuration
load_dotenv()
config = Code4meV2Config()

# Construct the broker URL for Redis message broker
broker_url = f"redis://{config.celery_broker_host}:{config.celery_broker_port}/0"
logging.info(f"Using Celery broker URL: {broker_url}")

# Construct the result backend URL for Redis result storage
result_backend_url = (
    f"redis://{config.celery_broker_host}:{config.celery_broker_port}/1"
)
logging.info(f"Using Celery result backend URL: {result_backend_url}")

# Initialize Celery application with Redis broker and backend
celery = Celery("Code4meV2 celery app", broker=broker_url, backend=result_backend_url)

# Configure task routing to distribute tasks across specialized queues
# Database tasks go to 'db' queue, LLM tasks go to 'llm' queue
celery.conf.task_routes = {
    "celery_app.tasks.db_tasks.*": {"queue": "db"},
    "celery_app.tasks.llm_tasks.*": {"queue": "llm"},
}

# Auto-discover and register tasks from specified modules
celery.autodiscover_tasks(
    [
        "celery_app.tasks.db_tasks",
        "celery_app.tasks.llm_tasks",
    ]
)
