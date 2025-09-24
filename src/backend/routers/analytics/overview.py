"""
Overview dashboard endpoints for high-level analytics summaries.

Provides endpoints for:
- System-wide overview metrics
- Recent activity summaries  
- Key performance indicators
- User engagement statistics
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from App import App
from .auth_utils import get_current_user, apply_user_filter, validate_time_range, AuthenticatedUser
from backend.Responses import JsonResponseWithStatus

router = APIRouter()


@router.get("/dashboard")
def get_dashboard_overview(
    time_window: str = Query("7d", description="Time window: 1d, 7d, 30d"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get main dashboard overview with key metrics.
    
    Returns high-level statistics for the specified time window including
    query counts, user activity, model performance, and trends.
    """
    db_session = app.get_db_session()
    
    try:
        # Calculate time range based on window
        end_time = datetime.now()
        window_map = {
            "1d": timedelta(days=1),
            "7d": timedelta(days=7), 
            "30d": timedelta(days=30)
        }
        
        if time_window not in window_map:
            raise HTTPException(status_code=400, detail="Invalid time window")
            
        start_time = end_time - window_map[time_window]
        
        # Apply user filtering
        query_params = {
            "start_time": start_time,
            "end_time": end_time
        }
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        # Get overview metrics
        overview_query = """
        WITH time_filtered_data AS (
            SELECT 
                mq.meta_query_id,
                mq.user_id,
                mq.query_type,
                mq.timestamp,
                mq.session_id,
                hg.was_accepted,
                hg.generation_time,
                hg.model_id
            FROM meta_query mq
            LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
            WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            overview_query += " AND mq.user_id = :user_id"
            
        overview_query += """
        )
        SELECT 
            COUNT(DISTINCT meta_query_id) as total_queries,
            COUNT(DISTINCT CASE WHEN query_type = 'completion' THEN meta_query_id END) as completion_queries,
            COUNT(DISTINCT CASE WHEN query_type = 'chat' THEN meta_query_id END) as chat_queries,
            COUNT(DISTINCT user_id) as active_users,
            COUNT(DISTINCT session_id) as total_sessions,
            AVG(CASE WHEN was_accepted IS NOT NULL THEN 
                CASE WHEN was_accepted THEN 1.0 ELSE 0.0 END END) as overall_acceptance_rate,
            AVG(generation_time) as avg_generation_time,
            COUNT(DISTINCT model_id) as models_used,
            COUNT(CASE WHEN was_accepted = true THEN 1 END) as total_accepted_generations
        FROM time_filtered_data
        """
        
        overview_result = db_session.execute(text(overview_query), query_params).fetchone()
        
        # Get trends (previous period comparison)
        prev_start_time = start_time - window_map[time_window]
        prev_end_time = start_time
        
        trend_query_params = {**query_params}
        trend_query_params["start_time"] = prev_start_time
        trend_query_params["end_time"] = prev_end_time
        
        trend_query = """
        WITH prev_period_data AS (
            SELECT 
                mq.meta_query_id,
                mq.user_id,
                mq.query_type,
                hg.was_accepted
            FROM meta_query mq
            LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
            WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            trend_query += " AND mq.user_id = :user_id"
            
        trend_query += """
        )
        SELECT 
            COUNT(DISTINCT meta_query_id) as prev_total_queries,
            COUNT(DISTINCT user_id) as prev_active_users,
            AVG(CASE WHEN was_accepted IS NOT NULL THEN 
                CASE WHEN was_accepted THEN 1.0 ELSE 0.0 END END) as prev_acceptance_rate
        FROM prev_period_data
        """
        
        trend_result = db_session.execute(text(trend_query), trend_query_params).fetchone()
        
        # Calculate trends
        def calculate_change(current, previous):
            if not previous or previous == 0:
                return 0.0
            return ((current - previous) / previous) * 100
        
        current_queries = overview_result.total_queries or 0
        current_users = overview_result.active_users or 0
        current_acceptance = overview_result.overall_acceptance_rate or 0.0
        
        prev_queries = trend_result.prev_total_queries or 0
        prev_users = trend_result.prev_active_users or 0  
        prev_acceptance = trend_result.prev_acceptance_rate or 0.0
        
        # Get top performing models
        top_models_query = """
        SELECT 
            mn.model_name,
            mn.model_id,
            COUNT(hg.meta_query_id) as usage_count,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            top_models_query += " AND mq.user_id = :user_id"
            
        top_models_query += """
        GROUP BY mn.model_name, mn.model_id
        HAVING COUNT(hg.meta_query_id) > 0
        ORDER BY acceptance_rate DESC, usage_count DESC
        LIMIT 5
        """
        
        top_models = db_session.execute(text(top_models_query), query_params).fetchall()
        
        # Get language breakdown
        language_query = """
        SELECT 
            pl.language_name,
            COUNT(mq.meta_query_id) as query_count,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate
        FROM meta_query mq
        LEFT JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
        LEFT JOIN programming_language pl ON ct.language_id = pl.language_id
        LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
          AND pl.language_name IS NOT NULL
        """
        
        if query_params.get("user_id"):
            language_query += " AND mq.user_id = :user_id"
            
        language_query += """
        GROUP BY pl.language_name
        ORDER BY query_count DESC
        LIMIT 10
        """
        
        languages = db_session.execute(text(language_query), query_params).fetchall()
        
        # Format response
        dashboard_data = {
            "time_window": time_window,
            "period": {
                "start": start_time.isoformat(),
                "end": end_time.isoformat()
            },
            "overview": {
                "total_queries": current_queries,
                "completion_queries": overview_result.completion_queries or 0,
                "chat_queries": overview_result.chat_queries or 0,
                "active_users": current_users,
                "total_sessions": overview_result.total_sessions or 0,
                "overall_acceptance_rate": float(current_acceptance),
                "avg_generation_time_ms": float(overview_result.avg_generation_time) if overview_result.avg_generation_time else 0.0,
                "models_used": overview_result.models_used or 0,
                "total_accepted_generations": overview_result.total_accepted_generations or 0
            },
            "trends": {
                "queries_change_pct": calculate_change(current_queries, prev_queries),
                "users_change_pct": calculate_change(current_users, prev_users),
                "acceptance_change_pct": calculate_change(current_acceptance, prev_acceptance),
                "direction": {
                    "queries": "up" if current_queries > prev_queries else "down" if current_queries < prev_queries else "stable",
                    "users": "up" if current_users > prev_users else "down" if current_users < prev_users else "stable", 
                    "acceptance": "up" if current_acceptance > prev_acceptance else "down" if current_acceptance < prev_acceptance else "stable"
                }
            },
            "top_models": [
                {
                    "model_id": model.model_id,
                    "model_name": model.model_name,
                    "usage_count": model.usage_count,
                    "acceptance_rate": float(model.acceptance_rate) if model.acceptance_rate else 0.0
                }
                for model in top_models
            ],
            "top_languages": [
                {
                    "language": lang.language_name,
                    "query_count": lang.query_count,
                    "acceptance_rate": float(lang.acceptance_rate) if lang.acceptance_rate else 0.0
                }
                for lang in languages
            ]
        }
        
        return JsonResponseWithStatus(
            status_code=200,
            content=dashboard_data
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving dashboard overview: {str(e)}")
    finally:
        db_session.close()


@router.get("/activity-timeline")
def get_activity_timeline(
    time_window: str = Query("24h", description="Time window: 1h, 6h, 24h, 7d"),
    granularity: str = Query("1h", description="Data granularity: 5m, 15m, 1h, 1d"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get activity timeline showing query volume and acceptance over time.
    
    Returns time-series data for creating activity charts and identifying usage patterns.
    """
    db_session = app.get_db_session()
    
    try:
        # Calculate time range
        end_time = datetime.now()
        window_map = {
            "1h": timedelta(hours=1),
            "6h": timedelta(hours=6),
            "24h": timedelta(hours=24),
            "7d": timedelta(days=7)
        }
        
        if time_window not in window_map:
            raise HTTPException(status_code=400, detail="Invalid time window")
            
        start_time = end_time - window_map[time_window]
        
        # Apply user filtering
        query_params = {
            "start_time": start_time,
            "end_time": end_time
        }
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        timeline_query = """
        SELECT 
            date_trunc(:granularity, mq.timestamp) as time_bucket,
            COUNT(mq.meta_query_id) as query_count,
            COUNT(CASE WHEN mq.query_type = 'completion' THEN 1 END) as completion_count,
            COUNT(CASE WHEN mq.query_type = 'chat' THEN 1 END) as chat_count,
            COUNT(DISTINCT mq.user_id) as active_users_in_bucket,
            AVG(CASE WHEN hg.was_accepted IS NOT NULL THEN 
                CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END END) as acceptance_rate,
            AVG(hg.generation_time) as avg_generation_time
        FROM meta_query mq
        LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            timeline_query += " AND mq.user_id = :user_id"
            
        timeline_query += """
        GROUP BY time_bucket
        ORDER BY time_bucket ASC
        """
        
        result = db_session.execute(
            text(timeline_query), 
            {**query_params, "granularity": granularity}
        ).fetchall()
        
        timeline_data = []
        for row in result:
            timeline_data.append({
                "time_bucket": row.time_bucket.isoformat(),
                "query_count": row.query_count,
                "completion_count": row.completion_count,
                "chat_count": row.chat_count,
                "active_users": row.active_users_in_bucket,
                "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                "avg_generation_time": float(row.avg_generation_time) if row.avg_generation_time else 0.0
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "timeline": timeline_data,
                "time_window": time_window,
                "granularity": granularity,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat()
                },
                "filters": {
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving activity timeline: {str(e)}")
    finally:
        db_session.close()


@router.get("/user-engagement")
def get_user_engagement_summary(
    time_window: str = Query("30d", description="Time window: 7d, 30d, 90d"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get user engagement summary metrics.
    
    Returns user activity patterns, retention, and engagement statistics.
    Admin users see aggregated stats, regular users see their personal stats.
    """
    db_session = app.get_db_session()
    
    try:
        # Calculate time range
        end_time = datetime.now()
        window_map = {
            "7d": timedelta(days=7),
            "30d": timedelta(days=30),
            "90d": timedelta(days=90)
        }
        
        if time_window not in window_map:
            raise HTTPException(status_code=400, detail="Invalid time window")
            
        start_time = end_time - window_map[time_window]
        
        # Apply user filtering
        query_params = {
            "start_time": start_time,
            "end_time": end_time
        }
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        if current_user.is_admin and not query_params.get("user_id"):
            # Admin view - aggregated statistics
            engagement_query = """
            WITH user_activity AS (
                SELECT 
                    mq.user_id,
                    COUNT(mq.meta_query_id) as query_count,
                    COUNT(DISTINCT DATE(mq.timestamp)) as active_days,
                    COUNT(DISTINCT mq.session_id) as session_count,
                    MIN(mq.timestamp) as first_query,
                    MAX(mq.timestamp) as last_query,
                    AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as user_acceptance_rate
                FROM meta_query mq
                LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
                WHERE mq.timestamp BETWEEN :start_time AND :end_time
                GROUP BY mq.user_id
            )
            SELECT 
                COUNT(DISTINCT user_id) as total_active_users,
                AVG(query_count) as avg_queries_per_user,
                AVG(active_days) as avg_active_days,
                AVG(session_count) as avg_sessions_per_user,
                AVG(user_acceptance_rate) as avg_user_acceptance_rate,
                COUNT(CASE WHEN query_count >= 10 THEN 1 END) as highly_active_users,
                COUNT(CASE WHEN active_days >= 5 THEN 1 END) as regular_users,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY query_count) as median_queries_per_user
            FROM user_activity
            """
            
            result = db_session.execute(text(engagement_query), query_params).fetchone()
            
            engagement_data = {
                "summary_type": "aggregated",
                "total_active_users": result.total_active_users or 0,
                "avg_queries_per_user": float(result.avg_queries_per_user) if result.avg_queries_per_user else 0.0,
                "median_queries_per_user": float(result.median_queries_per_user) if result.median_queries_per_user else 0.0,
                "avg_active_days": float(result.avg_active_days) if result.avg_active_days else 0.0,
                "avg_sessions_per_user": float(result.avg_sessions_per_user) if result.avg_sessions_per_user else 0.0,
                "avg_user_acceptance_rate": float(result.avg_user_acceptance_rate) if result.avg_user_acceptance_rate else 0.0,
                "highly_active_users": result.highly_active_users or 0,
                "regular_users": result.regular_users or 0
            }
            
        else:
            # Individual user view
            target_user_id = query_params.get("user_id") or str(current_user.user_id)
            
            personal_query = """
            SELECT 
                COUNT(mq.meta_query_id) as total_queries,
                COUNT(DISTINCT DATE(mq.timestamp)) as active_days,
                COUNT(DISTINCT mq.session_id) as total_sessions,
                MIN(mq.timestamp) as first_query,
                MAX(mq.timestamp) as last_query,
                AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
                COUNT(DISTINCT DATE(mq.timestamp)::text) as unique_days,
                AVG(EXTRACT(HOUR FROM mq.timestamp)) as avg_hour_of_day
            FROM meta_query mq
            LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
            WHERE mq.timestamp BETWEEN :start_time AND :end_time
              AND mq.user_id = :user_id
            """
            
            result = db_session.execute(
                text(personal_query), 
                {**query_params, "user_id": target_user_id}
            ).fetchone()
            
            engagement_data = {
                "summary_type": "personal",
                "user_id": target_user_id,
                "total_queries": result.total_queries or 0,
                "active_days": result.active_days or 0,
                "total_sessions": result.total_sessions or 0,
                "first_query": result.first_query.isoformat() if result.first_query else None,
                "last_query": result.last_query.isoformat() if result.last_query else None,
                "acceptance_rate": float(result.acceptance_rate) if result.acceptance_rate else 0.0,
                "avg_hour_of_day": float(result.avg_hour_of_day) if result.avg_hour_of_day else 12.0
            }
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "engagement": engagement_data,
                "time_window": time_window,
                "period": {
                    "start": start_time.isoformat(),
                    "end": end_time.isoformat()
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving user engagement: {str(e)}")
    finally:
        db_session.close()

