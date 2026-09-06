"""Agent sub-router: profiles, A/B assignments, telemetry ingestion, memory.

Mounted at ``/api/agent`` alongside ``backend.routers.agents`` (task lifecycle
and the inference relay), which shares the same prefix. The split is by
*authentication*, not by URL space:

* this package's ``ingest`` and ``memory`` routers authenticate the locally
  launched agent process via its ACP bearer token;
* ``profiles`` is admin/authenticated-user via ``auth_utils``;
* ``backend.routers.agents`` authenticates the IDE plugin via its session cookie.
"""

from fastapi import APIRouter

from .ingest import router as ingest_router
from .memory import router as memory_router
from .profiles import router as profiles_router

router = APIRouter()
router.include_router(profiles_router)
router.include_router(ingest_router)
router.include_router(memory_router)
