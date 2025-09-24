"""
Model analytics endpoints for AI model performance evaluation.

Provides endpoints for:
- Model comparison and benchmarking
- Quality metrics vs ground truth
- Model configuration analysis
- Generation performance tracking
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


@router.get("/comparison")
def get_model_comparison(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_ids: Optional[str] = Query(None, description="Comma-separated model IDs to compare"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Compare multiple models across key metrics.
    
    Returns side-by-side comparison of acceptance rates, latency, confidence, 
    and usage volume for specified models.
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
            mn.model_id,
            mn.model_name,
            mn.is_instruction_tuned,
            COUNT(*) as total_generations,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            AVG(hg.generation_time) as avg_generation_time,
            PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY hg.generation_time) as p50_generation_time,
            PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY hg.generation_time) as p95_generation_time,
            AVG(hg.confidence) as avg_confidence,
            STDDEV_POP(hg.confidence) as confidence_std,
            AVG(ARRAY_LENGTH(hg.logprobs, 1)) as avg_logprob_length,
            COUNT(DISTINCT mq.user_id) as unique_users,
            COUNT(DISTINCT DATE(mq.timestamp)) as active_days
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
            
        if model_ids:
            model_id_list = [int(x.strip()) for x in model_ids.split(",")]
            query += f" AND hg.model_id IN ({','.join(['%s'] * len(model_id_list))})"
            query_params.update({f"model_{i}": mid for i, mid in enumerate(model_id_list)})
        
        query += """
        GROUP BY mn.model_id, mn.model_name, mn.is_instruction_tuned
        ORDER BY acceptance_rate DESC
        """
        
        # Replace %s placeholders with proper SQLAlchemy parameters
        if model_ids:
            model_id_list = [int(x.strip()) for x in model_ids.split(",")]
            placeholders = ", ".join([f":model_{i}" for i in range(len(model_id_list))])
            query = query.replace(f"IN ({','.join(['%s'] * len(model_id_list))})", f"IN ({placeholders})")
            query_params.update({f"model_{i}": mid for i, mid in enumerate(model_id_list)})
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            data.append({
                "model_id": row.model_id,
                "model_name": row.model_name,
                "is_instruction_tuned": row.is_instruction_tuned,
                "metrics": {
                    "total_generations": row.total_generations,
                    "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                    "avg_generation_time": float(row.avg_generation_time) if row.avg_generation_time else 0.0,
                    "p50_generation_time": float(row.p50_generation_time) if row.p50_generation_time else 0.0,
                    "p95_generation_time": float(row.p95_generation_time) if row.p95_generation_time else 0.0,
                    "avg_confidence": float(row.avg_confidence) if row.avg_confidence else 0.0,
                    "confidence_std": float(row.confidence_std) if row.confidence_std else 0.0,
                    "avg_logprob_length": float(row.avg_logprob_length) if row.avg_logprob_length else 0.0,
                    "unique_users": row.unique_users,
                    "active_days": row.active_days
                }
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "model_ids": model_ids,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving model comparison: {str(e)}")
    finally:
        db_session.close()


@router.get("/ground-truth-analysis")
def get_ground_truth_analysis(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Analyze model quality against ground truth data.
    
    Returns exact match rates, edit distances, and quality metrics
    where ground truth data is available.
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
            mn.model_id,
            COUNT(*) as total_with_ground_truth,
            AVG(CASE WHEN gt.ground_truth = hg.completion THEN 1.0 ELSE 0.0 END) as exact_match_rate,
            AVG(LENGTH(hg.completion)) as avg_completion_length,
            AVG(LENGTH(gt.ground_truth)) as avg_ground_truth_length,
            AVG(hg.confidence) as avg_confidence_with_truth,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate_with_truth,
            -- Simple Levenshtein distance approximation using LENGTH difference
            AVG(ABS(LENGTH(hg.completion) - LENGTH(gt.ground_truth))) as avg_length_diff
        FROM completion_query cq
        JOIN ground_truth gt ON gt.completion_query_id = cq.meta_query_id
        JOIN had_generation hg ON hg.meta_query_id = cq.meta_query_id
        JOIN meta_query mq ON cq.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        WHERE gt.truth_timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += """
        GROUP BY mn.model_name, mn.model_id
        ORDER BY exact_match_rate DESC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            data.append({
                "model_id": row.model_id,
                "model_name": row.model_name,
                "total_with_ground_truth": row.total_with_ground_truth,
                "exact_match_rate": float(row.exact_match_rate) if row.exact_match_rate else 0.0,
                "avg_completion_length": float(row.avg_completion_length) if row.avg_completion_length else 0.0,
                "avg_ground_truth_length": float(row.avg_ground_truth_length) if row.avg_ground_truth_length else 0.0,
                "avg_confidence_with_truth": float(row.avg_confidence_with_truth) if row.avg_confidence_with_truth else 0.0,
                "acceptance_rate_with_truth": float(row.acceptance_rate_with_truth) if row.acceptance_rate_with_truth else 0.0,
                "avg_length_diff": float(row.avg_length_diff) if row.avg_length_diff else 0.0
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
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
        raise HTTPException(status_code=500, detail=f"Error retrieving ground truth analysis: {str(e)}")
    finally:
        db_session.close()


@router.get("/performance-by-context")
def get_performance_by_context(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Analyze model performance by contextual factors.
    
    Returns performance metrics segmented by document length, caret position,
    programming language, and trigger type.
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
            pl.language_name,
            tt.trigger_type_name,
            -- Segment documents by size
            CASE 
                WHEN ct.document_char_length < 1000 THEN 'small'
                WHEN ct.document_char_length < 10000 THEN 'medium'
                WHEN ct.document_char_length < 100000 THEN 'large'
                ELSE 'xlarge'
            END as document_size_category,
            -- Segment by relative position in document
            CASE 
                WHEN ct.relative_document_position < 0.25 THEN 'beginning'
                WHEN ct.relative_document_position < 0.75 THEN 'middle'
                ELSE 'end'
            END as document_position_category,
            COUNT(*) as sample_size,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            AVG(hg.generation_time) as avg_generation_time,
            AVG(hg.confidence) as avg_confidence
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        LEFT JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
        LEFT JOIN programming_language pl ON ct.language_id = pl.language_id
        LEFT JOIN trigger_type tt ON ct.trigger_type_id = tt.trigger_type_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
          AND ct.document_char_length IS NOT NULL
          AND ct.relative_document_position IS NOT NULL
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += """
        GROUP BY mn.model_name, pl.language_name, tt.trigger_type_name, 
                 document_size_category, document_position_category
        HAVING COUNT(*) >= 5  -- Only include segments with sufficient data
        ORDER BY mn.model_name, acceptance_rate DESC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            data.append({
                "model_name": row.model_name,
                "language": row.language_name,
                "trigger_type": row.trigger_type_name,
                "context": {
                    "document_size": row.document_size_category,
                    "document_position": row.document_position_category
                },
                "metrics": {
                    "sample_size": row.sample_size,
                    "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                    "avg_generation_time": float(row.avg_generation_time) if row.avg_generation_time else 0.0,
                    "avg_confidence": float(row.avg_confidence) if row.avg_confidence else 0.0
                }
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
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
        raise HTTPException(status_code=500, detail=f"Error retrieving performance by context: {str(e)}")
    finally:
        db_session.close()


@router.get("/model-usage-trends")
def get_model_usage_trends(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    granularity: str = Query("1d", description="Time granularity (1h, 1d)"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get model usage trends over time.
    
    Returns time-series data showing model adoption and usage patterns.
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
            date_trunc(:granularity, mq.timestamp) as time_bucket,
            mn.model_name,
            mn.model_id,
            COUNT(*) as usage_count,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            COUNT(DISTINCT mq.user_id) as unique_users
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
            
        query += """
        GROUP BY time_bucket, mn.model_name, mn.model_id
        ORDER BY time_bucket ASC, usage_count DESC
        """
        
        result = db_session.execute(
            text(query), 
            {**query_params, "granularity": granularity}
        ).fetchall()
        
        data = []
        for row in result:
            data.append({
                "time_bucket": row.time_bucket.isoformat(),
                "model_id": row.model_id,
                "model_name": row.model_name,
                "usage_count": row.usage_count,
                "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                "unique_users": row.unique_users
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "filters": {
                    "start_time": start_time,
                    "end_time": end_time,
                    "granularity": granularity,
                    "user_id": query_params.get("user_id")
                }
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving model usage trends: {str(e)}")
    finally:
        db_session.close()

