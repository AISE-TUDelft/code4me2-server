from fastapi import APIRouter

# Import sub-routers for different parts of the application
from .acp import router as acp_router
from .agent import router as agent_router
from .agents import router as agent_tasks_router
from .chat import router as chat_router
from .completion import router as completion_router
from .project import router as project_router
from .session import router as session_router
from .user import router as user_router
from .ws import router as ws_routers
from .analytics import router as analytics_router
from .config import router as config_router

# Main API router to aggregate and expose sub-routes
router = APIRouter()

# Include session-related endpoints
router.include_router(session_router, prefix="/session", tags=["Session"])

# Include project-related endpoints
router.include_router(project_router, prefix="/project", tags=["Project"])

# Include user-related endpoints
router.include_router(user_router, prefix="/user", tags=["User"])

# Include config management endpoints (admin only)
router.include_router(config_router, prefix="/config", tags=["Config"])

# Include code completion endpoints (no tags assigned here)
router.include_router(completion_router, prefix="/completion")

# Include chat-related endpoints
router.include_router(chat_router, prefix="/chat", tags=["Chat"])

# Include WebSocket-based endpoints (e.g., completions, chat)
router.include_router(ws_routers, prefix="/ws", tags=["WebSocket"])

# Include analytics and monitoring endpoints
router.include_router(analytics_router, prefix="/analytics", tags=["Analytics"])

# Agent subsystem. Both routers share the /agent prefix but authenticate
# differently: `agent_router` covers profiles + A/B assignments (admin auth) and
# self-report ingestion + memory (ACP bearer auth from the locally launched
# agent process), while `agent_tasks_router` covers task lifecycle and the
# inference relay (plugin session cookie).
router.include_router(agent_router, prefix="/agent", tags=["Agent"])
router.include_router(agent_tasks_router, prefix="/agent", tags=["Agent"])

# ACP grant handoff, so a locally launched agent process can authenticate.
router.include_router(acp_router, prefix="/acp", tags=["ACP"])


@router.api_route("/ping", methods=["HEAD"])
def ping():
    """
    Lightweight health check endpoint.

    Returns:
        dict: A simple response indicating the service is available.
    """
    return {"Status": "Ok"}
