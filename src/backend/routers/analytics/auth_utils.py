"""
Authentication and authorization utilities for analytics endpoints.

Provides common functionality for:
- User authentication verification
- Admin privilege checking
- Access control enforcement
- User data filtering
"""

import uuid
from typing import Optional, Dict, Any

from fastapi import HTTPException, Cookie, Depends
from sqlalchemy.orm import Session

import database.crud as crud
from App import App
from backend.redis_manager import RedisManager


class AuthenticatedUser:
    """Container for authenticated user information."""
    
    def __init__(self, user_id: uuid.UUID, is_admin: bool, email: str, name: str):
        self.user_id = user_id
        self.is_admin = is_admin
        self.email = email
        self.name = name


def get_current_user(
    auth_token: Optional[str] = Cookie(None, alias="auth_token"),
    app: App = Depends(App.get_instance)
) -> AuthenticatedUser:
    """
    Get current authenticated user from auth token.
    
    Args:
        auth_token: Authentication token from cookie
        app: Application instance with DB and Redis access
        
    Returns:
        AuthenticatedUser object with user information
        
    Raises:
        HTTPException: If user is not authenticated
    """
    if not auth_token:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    redis_manager = app.get_redis_manager()
    db_session = app.get_db_session()
    
    try:
        # Get user info from Redis token
        auth_info = redis_manager.get("auth_token", auth_token)
        if not auth_info:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        
        user_id = uuid.UUID(auth_info.get("user_id"))
        
        # Get full user details from database
        user = crud.get_user_by_id(db_session, user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        return AuthenticatedUser(
            user_id=user.user_id,
            is_admin=user.is_admin,
            email=user.email,
            name=user.name
        )
        
    except Exception as e:
        raise HTTPException(status_code=401, detail="Authentication failed")
    finally:
        db_session.close()


def require_admin(current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    """
    Require admin privileges for endpoint access.
    
    Args:
        current_user: Authenticated user from dependency
        
    Returns:
        AuthenticatedUser object if user is admin
        
    Raises:
        HTTPException: If user is not an admin
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    
    return current_user


def apply_user_filter(query_params: Dict[str, Any], current_user: AuthenticatedUser) -> Dict[str, Any]:
    """
    Apply user filtering to query parameters based on access level.
    
    For non-admin users, automatically filters to only their data.
    Admin users can specify user_id parameter or see all data.
    
    Args:
        query_params: Dictionary of query parameters
        current_user: Authenticated user information
        
    Returns:
        Modified query parameters with user filtering applied
    """
    if not current_user.is_admin:
        # Non-admin users can only see their own data
        query_params["user_id"] = str(current_user.user_id)
    
    return query_params


def validate_time_range(start_time: Optional[str], end_time: Optional[str]) -> tuple[str, str]:
    """
    Validate and provide defaults for time range parameters.
    
    Args:
        start_time: Start timestamp (ISO format)
        end_time: End timestamp (ISO format)
        
    Returns:
        Tuple of validated start and end times
        
    Raises:
        HTTPException: If time range is invalid
    """
    from datetime import datetime, timedelta
    
    try:
        if not end_time:
            end_time = datetime.now().isoformat()
        
        if not start_time:
            # Default to 30 days ago
            start_dt = datetime.now() - timedelta(days=30)
            start_time = start_dt.isoformat()
        
        # Validate format
        datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        
        return start_time, end_time
        
    except ValueError:
        raise HTTPException(
            status_code=400, 
            detail="Invalid time format. Use ISO 8601 format (YYYY-MM-DDTHH:MM:SS)"
        )

