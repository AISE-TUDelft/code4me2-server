"""
Main router for chat-related operations.

This module assembles all chat-related sub-routes under a single router.
Each sub-route is responsible for a specific type of operation:

- `/request`: Handles new chat completion requests.
- `/get`: Retrieves existing chats or their metadata.
- `/delete`: Deletes specific chats by ID.

These sub-routers are included under their respective prefixes to structure the API paths.
"""

from fastapi import APIRouter

from backend.routers.chat import delete, get, request

router = APIRouter()
router.include_router(request.router, prefix="/request")
router.include_router(get.router, prefix="/get")
router.include_router(delete.router, prefix="/delete")
