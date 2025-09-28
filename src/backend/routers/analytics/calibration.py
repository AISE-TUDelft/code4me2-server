"""
Model calibration analytics endpoints for confidence and reliability analysis.

Provides endpoints for:
- Reliability diagrams and calibration curves
- Expected Calibration Error (ECE) calculation
- Brier score and AUC/PR metrics
- Confidence distribution analysis
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
import math

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from App import App
from .auth_utils import get_current_user, apply_user_filter, validate_time_range, AuthenticatedUser
from backend.Responses import JsonResponseWithStatus

router = APIRouter()


@router.get("/reliability-diagram")
def get_reliability_diagram(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    bins: int = Query(10, description="Number of confidence bins", ge=5, le=20),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Generate reliability diagram data for model calibration analysis.
    
    Returns binned confidence vs actual acceptance rates for plotting
    reliability curves and calculating calibration metrics.
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
        WITH binned AS (
            SELECT 
                mn.model_name,
                mn.model_id,
                width_bucket(hg.confidence, 0.0, 1.0, :bins) as bin,
                hg.confidence,
                CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END as was_accepted
            FROM had_generation hg
            JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
            JOIN model_name mn ON hg.model_id = mn.model_id
            WHERE mq.timestamp BETWEEN :start_time AND :end_time
              AND hg.confidence IS NOT NULL
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += """
        )
        SELECT 
            model_name,
            model_id,
            bin,
            COUNT(*) as sample_size,
            AVG(confidence) as avg_predicted_confidence,
            AVG(was_accepted) as empirical_accuracy,
            MIN(confidence) as bin_min_confidence,
            MAX(confidence) as bin_max_confidence
        FROM binned
        GROUP BY model_name, model_id, bin
        ORDER BY model_name, bin
        """
        
        result = db_session.execute(
            text(query), 
            {**query_params, "bins": bins}
        ).fetchall()
        
        # Group by model and calculate ECE
        models_data = {}
        
        for row in result:
            model_key = f"{row.model_name}_{row.model_id}"
            if model_key not in models_data:
                models_data[model_key] = {
                    "model_id": row.model_id,
                    "model_name": row.model_name,
                    "bins": [],
                    "total_samples": 0
                }
            
            models_data[model_key]["bins"].append({
                "bin_number": row.bin,
                "sample_size": row.sample_size,
                "avg_predicted_confidence": float(row.avg_predicted_confidence) if row.avg_predicted_confidence else 0.0,
                "empirical_accuracy": float(row.empirical_accuracy) if row.empirical_accuracy else 0.0,
                "bin_range": {
                    "min": float(row.bin_min_confidence) if row.bin_min_confidence else 0.0,
                    "max": float(row.bin_max_confidence) if row.bin_max_confidence else 0.0
                }
            })
            models_data[model_key]["total_samples"] += row.sample_size
        
        # Calculate ECE for each model
        for model_data in models_data.values():
            total_samples = model_data["total_samples"]
            ece = 0.0
            
            for bin_data in model_data["bins"]:
                bin_weight = bin_data["sample_size"] / total_samples if total_samples > 0 else 0
                accuracy = bin_data["empirical_accuracy"]
                confidence = bin_data["avg_predicted_confidence"]
                ece += bin_weight * abs(accuracy - confidence)
            
            model_data["expected_calibration_error"] = ece
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": list(models_data.values()),
                "bins": bins,
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
        raise HTTPException(status_code=500, detail=f"Error generating reliability diagram: {str(e)}")
    finally:
        db_session.close()


@router.get("/brier-score")
def get_brier_score(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    group_by: str = Query("model", description="Group by: model, config, language"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Calculate Brier score for model probability calibration.
    
    Returns Brier scores grouped by specified dimension, measuring
    the accuracy of probabilistic predictions.
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
            "language": "pl.language_name"
        }
        
        if group_by not in group_columns:
            raise HTTPException(status_code=400, detail="Invalid group_by parameter")
        
        group_col = group_columns[group_by]
        
        query = f"""
        SELECT 
            {group_col} as group_name,
            mn.model_id,
            COUNT(*) as sample_size,
            -- Brier score: average of (probability - outcome)^2
            AVG(POWER(hg.confidence - CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END, 2)) as brier_score,
            -- Decompose Brier score
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as base_rate,
            AVG(hg.confidence) as avg_confidence,
            -- Reliability component: variance of confidence - accuracy per bin (simplified)
            AVG(POWER(hg.confidence - AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) OVER (), 2)) as reliability_component,
            -- Resolution component: variance of bin accuracies (simplified) 
            VARIANCE(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as resolution_component
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        JOIN "user" u ON mq.user_id = u.user_id
        JOIN config c ON u.config_id = c.config_id
        LEFT JOIN contextual_telemetry ct ON mq.contextual_telemetry_id = ct.contextual_telemetry_id
        LEFT JOIN programming_language pl ON ct.language_id = pl.language_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
          AND hg.confidence IS NOT NULL
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += f"""
        GROUP BY {group_col}, mn.model_id
        ORDER BY brier_score ASC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            # Calculate uncertainty (maximum possible Brier score)
            base_rate = float(row.base_rate) if row.base_rate else 0.0
            uncertainty = base_rate * (1 - base_rate)
            
            data.append({
                "group_name": row.group_name,
                "model_id": row.model_id,
                "sample_size": row.sample_size,
                "brier_score": float(row.brier_score) if row.brier_score else 0.0,
                "base_rate": base_rate,
                "avg_confidence": float(row.avg_confidence) if row.avg_confidence else 0.0,
                "uncertainty": uncertainty,
                "skill_score": (uncertainty - float(row.brier_score)) / uncertainty if uncertainty > 0 else 0.0,
                "reliability_component": float(row.reliability_component) if row.reliability_component else 0.0,
                "resolution_component": float(row.resolution_component) if row.resolution_component else 0.0
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": data,
                "group_by": group_by,
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
        raise HTTPException(status_code=500, detail=f"Error calculating Brier score: {str(e)}")
    finally:
        db_session.close()


@router.get("/confidence-distribution")
def get_confidence_distribution(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    bins: int = Query(20, description="Number of histogram bins", ge=10, le=50),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get confidence score distribution for models.
    
    Returns histogram data of confidence scores to understand
    model certainty patterns and over/under-confidence.
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
            width_bucket(hg.confidence, 0.0, 1.0, :bins) as confidence_bin,
            COUNT(*) as frequency,
            AVG(hg.confidence) as avg_bin_confidence,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as bin_acceptance_rate,
            MIN(hg.confidence) as bin_min,
            MAX(hg.confidence) as bin_max
        FROM had_generation hg
        JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
        JOIN model_name mn ON hg.model_id = mn.model_id
        WHERE mq.timestamp BETWEEN :start_time AND :end_time
          AND hg.confidence IS NOT NULL
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
        if model_id:
            query += " AND hg.model_id = :model_id"
            query_params["model_id"] = model_id
            
        query += """
        GROUP BY mn.model_name, mn.model_id, confidence_bin
        ORDER BY mn.model_name, confidence_bin
        """
        
        result = db_session.execute(
            text(query), 
            {**query_params, "bins": bins}
        ).fetchall()
        
        # Group by model
        models_data = {}
        
        for row in result:
            model_key = f"{row.model_name}_{row.model_id}"
            if model_key not in models_data:
                models_data[model_key] = {
                    "model_id": row.model_id,
                    "model_name": row.model_name,
                    "histogram": [],
                    "total_predictions": 0
                }
            
            models_data[model_key]["histogram"].append({
                "bin_number": row.confidence_bin,
                "frequency": row.frequency,
                "avg_bin_confidence": float(row.avg_bin_confidence) if row.avg_bin_confidence else 0.0,
                "bin_acceptance_rate": float(row.bin_acceptance_rate) if row.bin_acceptance_rate else 0.0,
                "range": {
                    "min": float(row.bin_min) if row.bin_min else 0.0,
                    "max": float(row.bin_max) if row.bin_max else 0.0
                }
            })
            models_data[model_key]["total_predictions"] += row.frequency
        
        # Calculate summary statistics for each model
        for model_data in models_data.values():
            total = model_data["total_predictions"]
            histogram = model_data["histogram"]
            
            # Calculate mean and std of confidence
            weighted_sum = sum(bin_data["avg_bin_confidence"] * bin_data["frequency"] for bin_data in histogram)
            mean_confidence = weighted_sum / total if total > 0 else 0.0
            
            # Calculate mode (bin with highest frequency)
            mode_bin = max(histogram, key=lambda x: x["frequency"]) if histogram else None
            
            model_data["statistics"] = {
                "mean_confidence": mean_confidence,
                "mode_confidence": mode_bin["avg_bin_confidence"] if mode_bin else 0.0,
                "most_frequent_bin": mode_bin["bin_number"] if mode_bin else 0
            }
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "data": list(models_data.values()),
                "bins": bins,
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
        raise HTTPException(status_code=500, detail=f"Error retrieving confidence distribution: {str(e)}")
    finally:
        db_session.close()


@router.get("/calibration-summary")
def get_calibration_summary(
    start_time: Optional[str] = Query(None, description="Start time (ISO format)"),
    end_time: Optional[str] = Query(None, description="End time (ISO format)"),
    user_id: Optional[str] = Query(None, description="Filter by user ID (admin only)"),
    current_user: AuthenticatedUser = Depends(get_current_user),
    app: App = Depends(App.get_instance)
):
    """
    Get calibration summary metrics for all models.
    
    Returns overview of ECE, Brier scores, and confidence statistics
    for quick model calibration assessment.
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
        WITH model_metrics AS (
            SELECT 
                mn.model_id,
                mn.model_name,
                COUNT(*) as total_predictions,
                AVG(hg.confidence) as avg_confidence,
                AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
                AVG(POWER(hg.confidence - CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END, 2)) as brier_score,
                STDDEV_POP(hg.confidence) as confidence_std,
                MIN(hg.confidence) as min_confidence,
                MAX(hg.confidence) as max_confidence
            FROM had_generation hg
            JOIN meta_query mq ON hg.meta_query_id = mq.meta_query_id
            JOIN model_name mn ON hg.model_id = mn.model_id
            WHERE mq.timestamp BETWEEN :start_time AND :end_time
              AND hg.confidence IS NOT NULL
        """
        
        if query_params.get("user_id"):
            query += " AND mq.user_id = :user_id"
            
        query += """
            GROUP BY mn.model_id, mn.model_name
        )
        SELECT * FROM model_metrics
        ORDER BY brier_score ASC
        """
        
        result = db_session.execute(text(query), query_params).fetchall()
        
        data = []
        for row in result:
            acceptance_rate = float(row.acceptance_rate) if row.acceptance_rate else 0.0
            avg_confidence = float(row.avg_confidence) if row.avg_confidence else 0.0
            
            # Calculate overconfidence/underconfidence
            confidence_gap = avg_confidence - acceptance_rate
            
            data.append({
                "model_id": row.model_id,
                "model_name": row.model_name,
                "total_predictions": row.total_predictions,
                "avg_confidence": avg_confidence,
                "acceptance_rate": acceptance_rate,
                "confidence_gap": confidence_gap,
                "is_overconfident": confidence_gap > 0.05,
                "is_underconfident": confidence_gap < -0.05,
                "brier_score": float(row.brier_score) if row.brier_score else 0.0,
                "confidence_std": float(row.confidence_std) if row.confidence_std else 0.0,
                "confidence_range": {
                    "min": float(row.min_confidence) if row.min_confidence else 0.0,
                    "max": float(row.max_confidence) if row.max_confidence else 0.0
                }
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
        raise HTTPException(status_code=500, detail=f"Error retrieving calibration summary: {str(e)}")
    finally:
        db_session.close()

