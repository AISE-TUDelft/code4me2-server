"""
Usage analytics endpoints for time-series and behavioral analysis.

Provides endpoints for:
- Query volume over time by various dimensions  
- Acceptance rate analysis
- Latency and performance metrics
- Session and user behavior patterns
"""

from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from App import App
from .auth_utils import get_current_user, apply_user_filter, validate_time_range, AuthenticatedUser
from backend.Responses import JsonResponseWithStatus

router = APIRouter()


@router.get("/queries-over-time")
def get_queries_over_time(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    granularity: str = Query("1h", description="Time granularity (1m, 5m, 1h, 1d)"),
    query_type: Optional[str] = Query(None, description="Filter by query type (chat, completion)"),
    language: Optional[str] = Query(None, description="Filter by programming language"),
    trigger_type: Optional[str] = Query(None, description="Filter by trigger type"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get query volume over time with various grouping dimensions.
    
    Returns time-series data showing query counts aggregated by specified granularity.
    Can be filtered and grouped by query type, programming language, trigger type, etc.
    """
    db_session = app.get_db_session()
    
    try:
        start_time, end_time = validate_time_range(start_time, end_time)
        
        # Apply user filtering for non-admin users
        query_params = {"start_time": start_time, "end_time": end_time}
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        # Build dynamic query based on filters
        base_query = """
        SELECT 
            date_trunc(:granularity, mq.timestamp) as time_bucket,
            pl.language_name,
            tt.trigger_type_name,
            mq.query_type,
            COUNT(*) as query_count
        FROM meta_query mq
        LEFT JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
        LEFT JOIN programming_language pl ON ct.language_id = pl.language_id
        LEFT JOIN trigger_type tt ON ct.trigger_type_id = tt.trigger_type_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        # Add filters
        if query_params.get("user_id"):
            base_query += " AND mq.user_id = :user_id"
        if query_type:
            base_query += " AND mq.query_type = :query_type"
        if language:
            base_query += " AND pl.language_name = :language"
        if trigger_type:
            base_query += " AND tt.trigger_type_name = :trigger_type"
            
        base_query += """
        GROUP BY 1, 2, 3, 4
        ORDER BY 1 ASC
        """
        
        # Execute query
        result = db_session.execute(
            text(base_query), 
            {
                **query_params,
                "granularity": granularity,
                "query_type": query_type,
                "language": language,
                "trigger_type": trigger_type
            }
        ).fetchall()
        
        # Format results
        data = []
        for row in result:
            data.append({
                "time_bucket": row.time_bucket.isoformat(),
                "language": row.language_name,
                "trigger_type": row.trigger_type_name,
                "query_type": row.query_type,
                "count": row.query_count
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "granularity": granularity,
                    "query_type": query_type,
                    "language": language,
                    "trigger_type": trigger_type,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving usage data: {str(e)}")
    finally:
        db_session.close()


@router.get("/acceptance-rates")
def get_acceptance_rates(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    group_by: str = Query("model", description="Group by: model, config, language, trigger"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get acceptance rates grouped by various dimensions.
    
    Returns acceptance rates with confidence intervals and sample sizes.
    """
    db_session = app.get_db_session()
    
    try:
        start_time, end_time = validate_time_range(start_time, end_time)
        
        # Apply user filtering
        query_params = {"start_time": start_time, "end_time": end_time}
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        # Build query based on grouping
        group_columns = {
            "model": "mn.model_name",
            "config": "c.config_id", 
            "language": "pl.language_name",
            "trigger": "tt.trigger_type_name"
        }
        
        if group_by not in group_columns:
            raise HTTPException(status_code=400, detail="Invalid group_by parameter")
        
        group_col = group_columns[group_by]
        
        query = f"""
        SELECT 
            {group_col} as group_name,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            COUNT(*) as sample_size,
            STDDEV_POP(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as std_dev
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        JOIN "user" u ON mq.user_id = u.user_id
        JOIN config c ON u.config_id = c.config_id
        LEFT JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
        LEFT JOIN programming_language pl ON ct.language_id = pl.language_id
        LEFT JOIN trigger_type tt ON ct.trigger_type_id = tt.trigger_type_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
            
        query += f"""
        GROUP BY {group_col}
        ORDER BY acceptance_rate DESC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        # Calculate confidence intervals
        data = []
        for row in result:
            # 95% confidence interval using normal approximation
            import math
            n = row.sample_size
            p = row.acceptance_rate or 0
            if n > 0 and p > 0 and p < 1:
                se = math.sqrt(p * (1 - p) / n)
                margin = 1.96 * se
                ci_lower = max(0, p - margin)
                ci_upper = min(1, p + margin)
            else:
                ci_lower = ci_upper = p
                
            data.append({
                "group_name": row.group_name,
                "acceptance_rate": float(p) if p else 0.0,
                "sample_size": row.sample_size,
                "confidence_interval": {
                    "lower": float(ci_lower),
                    "upper": float(ci_upper)
                }
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "group_by": group_by,
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving acceptance rates: {str(e)}")
    finally:
        db_session.close()


@router.get("/latency-distribution") 
def get_latency_distribution(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get latency distribution data for performance analysis.
    
    Returns latency percentiles and distribution data.
    """
    db_session = app.get_db_session()
    
    try:
        start_time, end_time = validate_time_range(start_time, end_time)
        
        # Apply user filtering
        query_params = {"start_time": start_time, "end_time": end_time}
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        query = """
        SELECT 
            mn.model_name,
            c.config_id,
            hg.generation_time,
            mq.total_serving_time,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY hg.generation_time) 
                OVER (PARTITION BY mn.model_name) as p50_generation,
            PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY hg.generation_time) 
                OVER (PARTITION BY mn.model_name) as p90_generation,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY hg.generation_time) 
                OVER (PARTITION BY mn.model_name) as p95_generation,
            PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY hg.generation_time) 
                OVER (PARTITION BY mn.model_name) as p99_generation
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        JOIN "user" u ON mq.user_id = u.user_id
        JOIN config c ON u.config_id = c.config_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += " ORDER BY hg.generation_time"
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        # Group data by model
        model_stats = {}
        raw_data = []
        
        for row in result:
            raw_data.append({
                "model_name": row.model_name,
                "config_id": row.config_id,
                "generation_time": row.generation_time,
                "total_serving_time": row.total_serving_time
            })
            
            if row.model_name not in model_stats:
                model_stats[row.model_name] = {
                    "model_name": row.model_name,
                    "p50": float(row.p50_generation) if row.p50_generation else 0,
                    "p90": float(row.p90_generation) if row.p90_generation else 0,
                    "p95": float(row.p95_generation) if row.p95_generation else 0,
                    "p99": float(row.p99_generation) if row.p99_generation else 0
                }
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "percentiles": list(model_stats.values()),
                "raw_data": raw_data[:1000],  # Limit raw data for performance
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "model_id": model_id,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving latency data: {str(e)}")
    finally:
        db_session.close()


@router.get("/user-behavior")
def get_user_behavior_metrics(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get user behavior and engagement metrics.
    
    Returns data on typing speed, time between actions, and engagement patterns.
    """
    db_session = app.get_db_session()
    
    try:
        start_time, end_time = validate_time_range(start_time, end_time)
        
        # Apply user filtering
        query_params = {"start_time": start_time, "end_time": end_time}
        if user_id:
            query_params["user_id"] = user_id
        query_params = apply_user_filter(query_params, current_user)
        
        query = """
        SELECT 
            u.user_id,
            u.name,
            AVG(bt.typing_speed) as avg_typing_speed,
            AVG(bt.time_since_last_shown) as avg_time_since_shown,
            AVG(bt.time_since_last_accepted) as avg_time_since_accepted,
            COUNT(DISTINCT mq.session_id) as session_count,
            COUNT(mq.meta_query_id) as total_queries,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            COUNT(DISTINCT DATE(mq.timestamp)) as active_days
        FROM meta_query mq
        JOIN "user" u ON mq.user_id = u.user_id
        LEFT JOIN behavioral_telemetry bt ON mq.behavioral_telemetry_id = bt.behavioral_telemetry_id
        LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
            
        query += """
        GROUP BY u.user_id, u.name
        ORDER BY total_queries DESC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            data.append({
                "user_id": str(row.user_id),
                "user_name": row.name,
                "avg_typing_speed": float(row.avg_typing_speed) if row.avg_typing_speed else 0.0,
                "avg_time_since_shown": row.avg_time_since_shown,
                "avg_time_since_accepted": row.avg_time_since_accepted,
                "session_count": row.session_count,
                "total_queries": row.total_queries,
                "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                "active_days": row.active_days
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving behavior metrics: {str(e)}")
    finally:
        db_session.close()

