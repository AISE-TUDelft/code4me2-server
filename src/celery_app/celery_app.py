import logging

# Set multiprocessing start method early for CUDA compatibility
import multiprocessing

multiprocessing.set_start_method("spawn", force=True)

import torch  # noqa: E402

torch.multiprocessing.set_start_method("spawn", force=True)
import os  # noqa: E402

import dotenv  # noqa: E402
from celery import Celery  # noqa: E402

# import celery_app.worker_init  # noqa

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
)

dotenv.load_dotenv()

broker_host = os.getenv("CELERY_BROKER_HOST", "localhost")
broker_port = os.getenv("CELERY_BROKER_PORT", "6379")
# Construct the broker URL
broker_url = f"redis://{broker_host}:{broker_port}/0"
logging.info(f"Using Celery broker URL: {broker_url}")
result_backend_url = f"redis://{broker_host}:{broker_port}/1"
logging.info(f"Using Celery result backend URL: {result_backend_url}")
celery = Celery("Code4meV2 celery app", broker=broker_url, backend=result_backend_url)

celery.conf.task_routes = {
    "celery_app.tasks.db_tasks.*": {"queue": "db"},
    "celery_app.tasks.llm_tasks.*": {"queue": "llm"},
}
celery.autodiscover_tasks(
    [
        "celery_app.tasks.db_tasks",
        "celery_app.tasks.llm_tasks",
    ]
)
# Automatically discover tasks from these packages
# celery.autodiscover_tasks(["celery_app.tasks"])
