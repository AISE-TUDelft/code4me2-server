import sys
from unittest.mock import MagicMock

# 👇 These mocks prevent actual Celery + Redis setup on import
sys.modules["celery"] = MagicMock()
sys.modules["celery_app.celery_app"] = MagicMock()
sys.modules["celery_app.tasks.db_tasks"] = MagicMock()
sys.modules["celery_app.tasks.llm_tasks"] = MagicMock()
