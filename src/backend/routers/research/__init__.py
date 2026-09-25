"""Research-platform sub-router (Issue 01+).

Mounted at ``/api/research``. The study protocol publication API is
researcher/admin-scoped: it defines experimental intent, so it is never exposed
to participants. Capability qualification is confined to the admin-only
``/packages`` operator/build surface; there is no public self-attestation route.
"""

from fastapi import APIRouter

from .agents import router as agents_router
from .bootstrap import router as bootstrap_router
from .join import router as join_router
from .operations import operations_router as pilot_operations_router
from .packages import packages_router
from .participants import router as participants_router
from .providers import router as providers_router
from .read_models import operations_router as read_operations_router
from .researchers import router as researchers_router
from .sessions import router as sessions_router
from .studies import router as studies_router
from .study_analytics import router as study_analytics_router
from .telemetry import router as telemetry_router

router = APIRouter()
router.include_router(
    studies_router,
    prefix="/studies",
    tags=["Research Studies"],
)
# Study-owner/administrator analytics read models: participants table,
# per-participant dashboard and arm comparison (``/{study_id}/analytics/...``).
router.include_router(
    study_analytics_router,
    prefix="/studies",
    tags=["Research Study Analytics"],
)
# Participant self-enrollment by shared join code. ``/join/{code}`` is readable
# by any authenticated user; ``POST /join`` enrolls the caller.
router.include_router(
    join_router,
    prefix="/join",
    tags=["Research Onboarding"],
)
# Administrator enablement of researcher accounts (the single researcher
# authority; no per-study role grants).
router.include_router(
    researchers_router,
    prefix="/researchers",
    tags=["Research Researchers"],
)
# Administrator-managed provider connections plus a researcher's granted subset.
router.include_router(
    providers_router,
    tags=["Research Provider Connections"],
)
router.include_router(
    agents_router,
    prefix="/agents",
    tags=["Research Agents"],
)
router.include_router(
    participants_router,
    prefix="/participants",
    tags=["Research Participants"],
)
router.include_router(
    bootstrap_router,
    prefix="/bootstrap",
    tags=["Research Bootstrap"],
)
router.include_router(
    sessions_router,
    prefix="/sessions",
    tags=["Research Sessions"],
)
router.include_router(
    telemetry_router,
    prefix="/telemetry",
    tags=["Research Telemetry"],
)
# One operations/read API surface: the RBAC-scoped researcher read models
# (enrollment coverage, telemetry coverage and
# metrics) and the admin-only pilot/release/kill-switch operations are merged
# into a single router mounted exactly once under /operations. Their handler
# paths are unchanged; authorization remains per-route.
operations_router = APIRouter()
operations_router.include_router(read_operations_router)
operations_router.include_router(pilot_operations_router)
router.include_router(
    operations_router,
    prefix="/operations",
    tags=["Research Operations"],
)
router.include_router(
    packages_router,
    prefix="/packages",
    tags=["Research Packages"],
)
