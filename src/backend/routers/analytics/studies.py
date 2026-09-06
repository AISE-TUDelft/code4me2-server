"""
A/B testing and study management endpoints.

Provides endpoints for:
- Creating and managing user studies
- Configuration assignment and randomization
- Study evaluation and statistical analysis
- Treatment comparison and uplift calculation

Admin-only endpoints for study lifecycle management.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
import uuid
import random

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import text, and_
from sqlalchemy.orm import Session
from pydantic import BaseModel

from App import App
from .auth_utils import require_admin, get_current_user, validate_time_range, AuthenticatedUser
from backend.Responses import JsonResponseWithStatus
import database.crud as crud

router = APIRouter()


class CreateStudyRequest(BaseModel):
    name: str
    description: Optional[str] = None
    starts_at: str  # ISO datetime
    ends_at: Optional[str] = None  # ISO datetime
    config_ids: List[int]  # List of configuration IDs to test
    default_config_id: int  # Configuration to use when study ends
    # Optional agent-profile arms. Empty → a completion-only study, behaving
    # exactly as before this field existed (backwards compatible).
    agent_profile_ids: List[str] = []  # agent_profile UUIDs acting as A/B arms
    # Which arm agent uplift is measured against; must be one of
    # agent_profile_ids when set. Analogous to default_config_id on the
    # completion side.
    baseline_agent_profile_id: Optional[str] = None


class UpdateStudyRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    ends_at: Optional[str] = None
    is_active: Optional[bool] = None


@router.post("/create")
def create_study(
    study_request: CreateStudyRequest,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Create a new user study for A/B testing.
    
    Only one study can be active at a time. Creating a new active study
    will deactivate any existing active studies.
    
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        # Validate datetime formats
        try:
            starts_at = datetime.fromisoformat(study_request.starts_at.replace('Z', '+00:00'))
            ends_at = None
            if study_request.ends_at:
                ends_at = datetime.fromisoformat(study_request.ends_at.replace('Z', '+00:00'))
                if ends_at <= starts_at:
                    raise HTTPException(status_code=400, detail="End time must be after start time")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid datetime format. Use ISO 8601 format")
        
        # Validate config IDs exist
        for config_id in study_request.config_ids + [study_request.default_config_id]:
            config = crud.get_config_by_id(db_session, config_id)
            if not config:
                raise HTTPException(status_code=400, detail=f"Configuration {config_id} not found")

        # Validate the optional agent-profile arms. Done before any writes so a
        # bad arm list can't leave a half-configured study behind.
        agent_profile_uuids: List[uuid.UUID] = []
        baseline_profile_uuid: Optional[uuid.UUID] = None
        try:
            agent_profile_uuids = [
                uuid.UUID(pid) for pid in study_request.agent_profile_ids
            ]
            if study_request.baseline_agent_profile_id is not None:
                baseline_profile_uuid = uuid.UUID(
                    study_request.baseline_agent_profile_id
                )
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid agent profile ID format")

        for profile_uuid in agent_profile_uuids:
            if crud.get_agent_profile_by_id(db_session, profile_uuid) is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Agent profile {profile_uuid} not found",
                )
        if (
            baseline_profile_uuid is not None
            and baseline_profile_uuid not in agent_profile_uuids
        ):
            raise HTTPException(
                status_code=400,
                detail="Baseline agent profile must be one of the selected profiles",
            )

        # Check if there's already an active study
        active_study_query = """
        SELECT study_id FROM study WHERE is_active = true LIMIT 1
        """
        active_study = db_session.execute(text(active_study_query)).fetchone()
        
        if active_study and starts_at <= datetime.now():
            # Deactivate existing active study
            deactivate_query = """
            UPDATE study SET is_active = false WHERE is_active = true
            """
            db_session.execute(text(deactivate_query))
        
        # Create new study
        study_id = uuid.uuid4()
        is_active = starts_at <= datetime.now() and (ends_at is None or ends_at > datetime.now())
        
        create_query = """
        INSERT INTO study (study_id, name, description, created_by, starts_at, ends_at, is_active, default_config_id, created_at)
        VALUES (:study_id, :name, :description, :created_by, :starts_at, :ends_at, :is_active, :default_config_id, :created_at)
        """
        
        db_session.execute(text(create_query), {
            "study_id": study_id,
            "name": study_request.name,
            "description": study_request.description,
            "created_by": current_user.user_id,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "is_active": is_active,
            "default_config_id": study_request.default_config_id,
            "created_at": datetime.now()
        })
        
        # If study is active, assign configurations to users
        if is_active:
            assign_users_to_study(db_session, study_id, study_request.config_ids)

        # Attach the agent-profile arms. Persisted regardless of active state:
        # only an *active* study's arms are drawn from
        # (crud.list_active_study_agent_profiles), and agent assignment is
        # sticky-lazy — drawn on a user's first agent task rather than
        # pre-assigned like completion configs — so there's nothing to backfill
        # here.
        if agent_profile_uuids:
            crud.set_study_agent_profiles(
                db_session,
                study_id=study_id,
                profile_ids=agent_profile_uuids,
                baseline_profile_id=baseline_profile_uuid,
            )

        db_session.commit()

        return JsonResponseWithStatus(
            status_code=201,
            content={
                "study_id": str(study_id),
                "name": study_request.name,
                "is_active": is_active,
                "agent_profile_ids": [str(p) for p in agent_profile_uuids],
                "baseline_agent_profile_id": (
                    str(baseline_profile_uuid) if baseline_profile_uuid else None
                ),
                "message": "Study created successfully"
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error creating study: {str(e)}")
    finally:
        db_session.close()


def assign_users_to_study(db_session: Session, study_id: uuid.UUID, config_ids: List[int]):
    """
    Randomly assign users to configurations for a study.
    
    Args:
        db_session: Database session
        study_id: Study UUID
        config_ids: List of configuration IDs to assign
    """
    # Get all active users
    users_query = """
    SELECT user_id FROM "user" WHERE verified = true
    """
    users = db_session.execute(text(users_query)).fetchall()
    
    # Randomly assign users to configurations
    for user in users:
        assigned_config_id = random.choice(config_ids)
        
        # Insert assignment record
        assignment_query = """
        INSERT INTO config_assignment_history (user_id, study_id, assigned_config_id, assigned_at)
        VALUES (:user_id, :study_id, :assigned_config_id, :assigned_at)
        ON CONFLICT (user_id, study_id) DO UPDATE SET 
            assigned_config_id = :assigned_config_id,
            assigned_at = :assigned_at
        """
        
        db_session.execute(text(assignment_query), {
            "user_id": user.user_id,
            "study_id": study_id,
            "assigned_config_id": assigned_config_id,
            "assigned_at": datetime.now()
        })
        
        # Update user's current config
        update_user_config_query = """
        UPDATE "user" SET config_id = :config_id WHERE user_id = :user_id
        """
        
        db_session.execute(text(update_user_config_query), {
            "config_id": assigned_config_id,
            "user_id": user.user_id
        })


@router.get("/list")
def list_studies(
    include_inactive: bool = Query(False, description="Include inactive studies"),
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    List all studies with basic information.
    
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        query = """
        SELECT 
            s.study_id,
            s.name,
            s.description,
            s.created_by,
            s.starts_at,
            s.ends_at,
            s.is_active,
            s.default_config_id,
            s.created_at,
            u.name as creator_name,
            COUNT(DISTINCT cah.user_id) as assigned_users_count
        FROM study s
        JOIN "user" u ON s.created_by = u.user_id
        LEFT JOIN config_assignment_history cah ON s.study_id = cah.study_id
        """
        
        if not include_inactive:
            query += " WHERE s.is_active = true"
            
        query += """
        GROUP BY s.study_id, s.name, s.description, s.created_by, s.starts_at, s.ends_at, 
                 s.is_active, s.default_config_id, s.created_at, u.name
        ORDER BY s.created_at DESC
        """
        
        result = db_session.execute(text(query)).fetchall()
        
        studies = []
        for row in result:
            studies.append({
                "study_id": str(row.study_id),
                "name": row.name,
                "description": row.description,
                "creator_name": row.creator_name,
                "starts_at": row.starts_at.isoformat(),
                "ends_at": row.ends_at.isoformat() if row.ends_at else None,
                "is_active": row.is_active,
                "default_config_id": row.default_config_id,
                "assigned_users_count": row.assigned_users_count,
                "created_at": row.created_at.isoformat()
            })
        
        return JsonResponseWithStatus(
            status_code=200,
            content={"studies": studies}
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error listing studies: {str(e)}")
    finally:
        db_session.close()


@router.get("/{study_id}/details")
def get_study_details(
    study_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Get detailed information about a specific study.
    
    Includes configuration assignments and participant statistics.
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid study ID format")
    
    try:
        # Get study basic info
        study_query = """
        SELECT 
            s.study_id,
            s.name,
            s.description,
            s.created_by,
            s.starts_at,
            s.ends_at,
            s.is_active,
            s.default_config_id,
            s.created_at,
            u.name as creator_name
        FROM study s
        JOIN "user" u ON s.created_by = u.user_id
        WHERE s.study_id = :study_id
        """
        
        study = db_session.execute(text(study_query), {"study_id": study_uuid}).fetchone()
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        
        # Get configuration assignments
        assignments_query = """
        SELECT 
            cah.assigned_config_id,
            COUNT(*) as user_count,
            AVG(
                CASE 
                    WHEN mq.user_id IS NOT NULL THEN 1.0 
                    ELSE 0.0 
                END
            ) as engagement_rate
        FROM config_assignment_history cah
        LEFT JOIN meta_query mq ON cah.user_id = mq.user_id 
            AND mq.timestamp BETWEEN :starts_at AND COALESCE(:ends_at, NOW())
        WHERE cah.study_id = :study_id
        GROUP BY cah.assigned_config_id
        ORDER BY cah.assigned_config_id
        """
        
        assignments = db_session.execute(text(assignments_query), {
            "study_id": study_uuid,
            "starts_at": study.starts_at,
            "ends_at": study.ends_at
        }).fetchall()
        
        assignment_data = []
        for assignment in assignments:
            assignment_data.append({
                "config_id": assignment.assigned_config_id,
                "user_count": assignment.user_count,
                "engagement_rate": float(assignment.engagement_rate) if assignment.engagement_rate else 0.0
            })
        
        # Agent-profile arms attached to this study. Empty for completion-only
        # studies, which is the backwards-compatible default.
        agent_profiles = [
            {
                "profile_id": str(link.profile_id),
                "name": link.profile.name if link.profile else None,
                "model": link.profile.model if link.profile else None,
                "framework_version": (
                    link.profile.framework_version if link.profile else None
                ),
                "is_active": link.profile.is_active if link.profile else None,
                "is_baseline": link.is_baseline,
            }
            for link in crud.list_study_agent_profiles(db_session, study_uuid)
        ]

        return JsonResponseWithStatus(
            status_code=200,
            content={
                "study": {
                    "study_id": str(study.study_id),
                    "name": study.name,
                    "description": study.description,
                    "creator_name": study.creator_name,
                    "starts_at": study.starts_at.isoformat(),
                    "ends_at": study.ends_at.isoformat() if study.ends_at else None,
                    "is_active": study.is_active,
                    "default_config_id": study.default_config_id,
                    "created_at": study.created_at.isoformat()
                },
                "assignments": assignment_data,
                "agent_profiles": agent_profiles
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error retrieving study details: {str(e)}")
    finally:
        db_session.close()


@router.post("/{study_id}/activate")
def activate_study(
    study_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Activate a study and assign users to configurations.
    
    Deactivates any currently active study and assigns all users
    to the configurations defined in this study.
    
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid study ID format")
    
    try:
        # Get study details
        study_query = """
        SELECT study_id, name, starts_at, ends_at FROM study WHERE study_id = :study_id
        """
        study = db_session.execute(text(study_query), {"study_id": study_uuid}).fetchone()
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        
        # Check if study can be activated (not ended)
        now = datetime.now(study.ends_at.tzinfo if study.ends_at else None)
        if study.ends_at and study.ends_at < now:
            raise HTTPException(status_code=400, detail="Cannot activate an ended study")
        
        # Deactivate any currently active study
        deactivate_query = """
        UPDATE study SET is_active = false WHERE is_active = true
        """
        db_session.execute(text(deactivate_query))
        
        # Activate this study
        activate_query = """
        UPDATE study SET is_active = true WHERE study_id = :study_id
        """
        db_session.execute(text(activate_query), {"study_id": study_uuid})
        
        # Get existing config assignments for this study
        existing_assignments_query = """
        SELECT DISTINCT assigned_config_id FROM config_assignment_history 
        WHERE study_id = :study_id
        """
        config_ids_result = db_session.execute(text(existing_assignments_query), {"study_id": study_uuid}).fetchall()
        
        if config_ids_result:
            config_ids = [row.assigned_config_id for row in config_ids_result]
            assign_users_to_study(db_session, study_uuid, config_ids)
        
        db_session.commit()
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "message": f"Study '{study.name}' activated successfully",
                "study_id": str(study_uuid)
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error activating study: {str(e)}")
    finally:
        db_session.close()


@router.post("/{study_id}/deactivate")
def deactivate_study(
    study_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Deactivate a study and reset all users to default configuration.
    
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid study ID format")
    
    try:
        # Get study details
        study_query = """
        SELECT study_id, name, default_config_id FROM study 
        WHERE study_id = :study_id AND is_active = true
        """
        study = db_session.execute(text(study_query), {"study_id": study_uuid}).fetchone()
        if not study:
            raise HTTPException(status_code=404, detail="Active study not found")
        
        # Deactivate study
        deactivate_query = """
        UPDATE study SET is_active = false, ends_at = COALESCE(ends_at, NOW()) 
        WHERE study_id = :study_id
        """
        db_session.execute(text(deactivate_query), {"study_id": study_uuid})
        
        # Reset all users to default configuration
        reset_configs_query = """
        UPDATE "user" SET config_id = :default_config_id
        WHERE user_id IN (
            SELECT user_id FROM config_assignment_history WHERE study_id = :study_id
        )
        """
        
        result = db_session.execute(text(reset_configs_query), {
            "default_config_id": study.default_config_id,
            "study_id": study_uuid
        })
        
        db_session.commit()
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "message": f"Study '{study.name}' deactivated successfully",
                "users_reset": result.rowcount,
                "default_config_id": study.default_config_id
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error deactivating study: {str(e)}")
    finally:
        db_session.close()


@router.get("/{study_id}/evaluation")
def evaluate_study(
    study_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Evaluate study results with statistical analysis.
    
    Returns A/B test results including uplift calculations, confidence intervals,
    and significance tests between different configurations.
    
    Admin only endpoint.
    """
    db_session = app.get_db_session()
    
    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid study ID format")
    
    try:
        # Get study details
        study_query = """
        SELECT study_id, name, starts_at, ends_at, default_config_id FROM study 
        WHERE study_id = :study_id
        """
        study = db_session.execute(text(study_query), {"study_id": study_uuid}).fetchone()
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")
        
        # Get study results by configuration
        evaluation_query = """
        SELECT 
            cah.assigned_config_id,
            COUNT(DISTINCT cah.user_id) as total_users,
            COUNT(DISTINCT mq.user_id) as active_users,
            COUNT(mq.meta_query_id) as total_queries,
            AVG(CASE WHEN hg.was_accepted THEN 1.0 ELSE 0.0 END) as acceptance_rate,
            COUNT(CASE WHEN hg.was_accepted THEN 1 END) as total_accepted,
            COUNT(hg.meta_query_id) as total_generations,
            AVG(hg.generation_time) as avg_generation_time,
            AVG(hg.confidence) as avg_confidence,
            COUNT(DISTINCT mq.session_id) as total_sessions,
            AVG(mq.total_serving_time) as avg_serving_time
        FROM config_assignment_history cah
        LEFT JOIN meta_query mq ON cah.user_id = mq.user_id 
            AND mq.timestamp BETWEEN :starts_at AND COALESCE(:ends_at, NOW())
        LEFT JOIN had_generation hg ON mq.meta_query_id = hg.meta_query_id
        WHERE cah.study_id = :study_id
        GROUP BY cah.assigned_config_id
        ORDER BY cah.assigned_config_id
        """
        
        results = db_session.execute(text(evaluation_query), {
            "study_id": study_uuid,
            "starts_at": study.starts_at,
            "ends_at": study.ends_at
        }).fetchall()
        
        if not results:
            raise HTTPException(status_code=404, detail="No study data found")
        
        # Calculate statistical metrics
        config_results = []
        baseline_config = None
        
        for row in results:
            config_data = {
                "config_id": row.assigned_config_id,
                "is_baseline": row.assigned_config_id == study.default_config_id,
                "metrics": {
                    "total_users": row.total_users,
                    "active_users": row.active_users or 0,
                    "activation_rate": (row.active_users or 0) / max(row.total_users, 1),
                    "total_queries": row.total_queries or 0,
                    "total_generations": row.total_generations or 0,
                    "acceptance_rate": float(row.acceptance_rate) if row.acceptance_rate else 0.0,
                    "total_accepted": row.total_accepted or 0,
                    "avg_generation_time": float(row.avg_generation_time) if row.avg_generation_time else 0.0,
                    "avg_confidence": float(row.avg_confidence) if row.avg_confidence else 0.0,
                    "total_sessions": row.total_sessions or 0,
                    "avg_serving_time": float(row.avg_serving_time) if row.avg_serving_time else 0.0
                }
            }
            
            if config_data["is_baseline"]:
                baseline_config = config_data
            
            config_results.append(config_data)
        
        # Calculate uplift vs baseline if baseline exists
        if baseline_config:
            baseline_acceptance = baseline_config["metrics"]["acceptance_rate"]
            baseline_generation_time = baseline_config["metrics"]["avg_generation_time"]
            
            for config_data in config_results:
                if not config_data["is_baseline"]:
                    config_acceptance = config_data["metrics"]["acceptance_rate"]
                    config_generation_time = config_data["metrics"]["avg_generation_time"]
                    
                    # Calculate uplift percentages
                    acceptance_uplift = ((config_acceptance - baseline_acceptance) / max(baseline_acceptance, 0.001)) * 100
                    generation_time_change = ((config_generation_time - baseline_generation_time) / max(baseline_generation_time, 1)) * 100
                    
                    config_data["vs_baseline"] = {
                        "acceptance_rate_uplift_pct": acceptance_uplift,
                        "generation_time_change_pct": generation_time_change,
                        "is_better_acceptance": config_acceptance > baseline_acceptance,
                        "is_faster": config_generation_time < baseline_generation_time
                    }
        
        return JsonResponseWithStatus(
            status_code=200,
            content={
                "study": {
                    "study_id": str(study.study_id),
                    "name": study.name,
                    "starts_at": study.starts_at.isoformat(),
                    "ends_at": study.ends_at.isoformat() if study.ends_at else None,
                    "default_config_id": study.default_config_id
                },
                "results": config_results
            }
        )
        
    except Exception as e:
        db_session.rollback()
        raise HTTPException(status_code=500, detail=f"Error evaluating study: {str(e)}")
    finally:
        db_session.close()



@router.get("/{study_id}/agent-evaluation")
def evaluate_study_agents(
    study_id: str,
    current_user: AuthenticatedUser = Depends(require_admin),
    app: App = Depends(App.get_instance)
):
    """
    Evaluate an A/B study's *agent* arms.

    The agent-side mirror of ``evaluate_study``: aggregates agent telemetry
    (``agent_task`` + ``agent_event``) per attached agent profile, within the
    study's time window, and reports uplift against the baseline arm.

    A task counts toward an arm when its ``agent_profile`` name matches a study
    profile and it was created inside ``[starts_at, ends_at]``. Matching by name
    rather than profile_id is deliberate — the name is what gets snapshotted
    onto the task, so historical tasks stay attributable even if the profile row
    is later edited or deleted.

    Because both telemetry paths write into the same ``agent_event`` table, the
    same aggregation works whether an arm ran Goose, Codex, or the built-in
    ``code4me2-agent`` runtime — which is the whole point of the unified schema.

    Admin only endpoint.
    """
    db_session = app.get_db_session()

    try:
        study_uuid = uuid.UUID(study_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid study ID format")

    try:
        study_query = """
        SELECT study_id, name, starts_at, ends_at
        FROM study WHERE study_id = :study_id
        """
        study = db_session.execute(
            text(study_query), {"study_id": study_uuid}
        ).fetchone()
        if not study:
            raise HTTPException(status_code=404, detail="Study not found")

        window = {
            "study_id": study_uuid,
            "starts_at": study.starts_at,
            "ends_at": study.ends_at,
        }

        # Per-arm task-level aggregates. LEFT JOIN so an arm with zero tasks
        # still appears as a row with zeros, rather than vanishing from the
        # comparison (an arm nobody used is itself a finding).
        task_query = """
        SELECT
            ap.profile_id,
            ap.name AS profile_name,
            ap.model,
            ap.framework_version,
            sap.is_baseline,
            COUNT(DISTINCT t.task_id) AS total_tasks,
            COUNT(DISTINCT t.task_id) FILTER (WHERE t.status = 'done') AS completed_tasks,
            COUNT(DISTINCT COALESCE(t.owner_user_id, t.session_id)) AS total_participants,
            AVG(t.total_steps) AS avg_steps,
            SUM(t.input_tokens) AS total_input_tokens,
            SUM(t.output_tokens) AS total_output_tokens
        FROM study_agent_profile sap
        JOIN agent_profile ap ON ap.profile_id = sap.profile_id
        LEFT JOIN agent_task t
            ON t.agent_profile = ap.name
            AND t.created_at BETWEEN :starts_at AND COALESCE(:ends_at, NOW())
        WHERE sap.study_id = :study_id
        GROUP BY ap.profile_id, ap.name, ap.model, ap.framework_version, sap.is_baseline
        ORDER BY ap.name
        """
        task_rows = db_session.execute(text(task_query), window).fetchall()

        # Per-arm event-level aggregates, kept as a separate query so the task
        # aggregates above aren't inflated by the event-row fan-out.
        event_query = """
        SELECT
            ap.name AS profile_name,
            COUNT(e.event_id) FILTER (WHERE e.event_type = 'model_call') AS model_calls,
            COUNT(e.event_id) FILTER (WHERE e.event_type = 'tool_call') AS tool_calls,
            AVG(e.latency_ms) FILTER (WHERE e.event_type = 'model_call') AS avg_model_latency_ms,
            SUM(e.total_tokens) AS total_tokens
        FROM study_agent_profile sap
        JOIN agent_profile ap ON ap.profile_id = sap.profile_id
        LEFT JOIN agent_task t
            ON t.agent_profile = ap.name
            AND t.created_at BETWEEN :starts_at AND COALESCE(:ends_at, NOW())
        LEFT JOIN agent_event e ON e.task_id = t.task_id
        WHERE sap.study_id = :study_id
        GROUP BY ap.name
        """
        event_rows = {
            row.profile_name: row
            for row in db_session.execute(text(event_query), window).fetchall()
        }

        # Human-in-the-loop signal: what the developer actually did with the
        # agent's proposals. This is the metric group-5 couldn't report at all,
        # and it's arguably the most important one — an arm that proposes more
        # edits but gets fewer accepted is worse, not better.
        edit_query = """
        SELECT
            ap.name AS profile_name,
            COUNT(ed.edit_id) AS total_edits,
            COUNT(ed.edit_id) FILTER (WHERE ed.was_accepted IS TRUE) AS accepted_edits,
            COUNT(ed.edit_id) FILTER (WHERE ed.was_accepted IS FALSE) AS rejected_edits,
            COUNT(ed.edit_id) FILTER (WHERE ed.was_modified IS TRUE) AS modified_edits
        FROM study_agent_profile sap
        JOIN agent_profile ap ON ap.profile_id = sap.profile_id
        LEFT JOIN agent_task t
            ON t.agent_profile = ap.name
            AND t.created_at BETWEEN :starts_at AND COALESCE(:ends_at, NOW())
        LEFT JOIN agent_edit ed ON ed.task_id = t.task_id
        WHERE sap.study_id = :study_id
        GROUP BY ap.name
        """
        edit_rows = {
            row.profile_name: row
            for row in db_session.execute(text(edit_query), window).fetchall()
        }

        def _f(value) -> float:
            return float(value) if value is not None else 0.0

        def _i(value) -> int:
            return int(value) if value is not None else 0

        arm_results = []
        baseline_arm = None

        for row in task_rows:
            events = event_rows.get(row.profile_name)
            edits = edit_rows.get(row.profile_name)
            total_tasks = _i(row.total_tasks)
            total_edits = _i(edits.total_edits) if edits else 0
            accepted_edits = _i(edits.accepted_edits) if edits else 0

            arm = {
                "profile_id": str(row.profile_id),
                "profile_name": row.profile_name,
                "model": row.model,
                "framework_version": row.framework_version,
                "is_baseline": bool(row.is_baseline),
                "metrics": {
                    "total_tasks": total_tasks,
                    "completed_tasks": _i(row.completed_tasks),
                    "completion_rate": _i(row.completed_tasks) / max(total_tasks, 1),
                    "total_participants": _i(row.total_participants),
                    "avg_steps": _f(row.avg_steps),
                    "total_input_tokens": _i(row.total_input_tokens),
                    "total_output_tokens": _i(row.total_output_tokens),
                    "model_calls": _i(events.model_calls) if events else 0,
                    "tool_calls": _i(events.tool_calls) if events else 0,
                    "avg_model_latency_ms": (
                        _f(events.avg_model_latency_ms) if events else 0.0
                    ),
                    "total_tokens": _i(events.total_tokens) if events else 0,
                    "total_edits": total_edits,
                    "accepted_edits": accepted_edits,
                    "rejected_edits": _i(edits.rejected_edits) if edits else 0,
                    "modified_edits": _i(edits.modified_edits) if edits else 0,
                    # Undecided edits are excluded from the denominator: an edit
                    # the developer hasn't ruled on yet is not a rejection.
                    "edit_acceptance_rate": (
                        accepted_edits
                        / max(
                            accepted_edits + (_i(edits.rejected_edits) if edits else 0),
                            1,
                        )
                    ),
                },
            }

            if arm["is_baseline"]:
                baseline_arm = arm
            arm_results.append(arm)

        # Uplift vs the baseline arm, when one is designated.
        if baseline_arm:
            base = baseline_arm["metrics"]
            for arm in arm_results:
                if arm["is_baseline"]:
                    continue
                metrics = arm["metrics"]
                arm["uplift"] = {
                    "completion_rate_change_pct": _pct_change(
                        metrics["completion_rate"], base["completion_rate"]
                    ),
                    "edit_acceptance_change_pct": _pct_change(
                        metrics["edit_acceptance_rate"], base["edit_acceptance_rate"]
                    ),
                    "avg_steps_change_pct": _pct_change(
                        metrics["avg_steps"], base["avg_steps"]
                    ),
                    "latency_change_pct": _pct_change(
                        metrics["avg_model_latency_ms"], base["avg_model_latency_ms"]
                    ),
                    "is_better_completion": (
                        metrics["completion_rate"] > base["completion_rate"]
                    ),
                    "is_better_acceptance": (
                        metrics["edit_acceptance_rate"] > base["edit_acceptance_rate"]
                    ),
                    "is_faster": (
                        metrics["avg_model_latency_ms"] < base["avg_model_latency_ms"]
                    ),
                }

        return JsonResponseWithStatus(
            status_code=200,
            content={
                "study": {
                    "study_id": str(study.study_id),
                    "name": study.name,
                    "starts_at": study.starts_at.isoformat(),
                    "ends_at": study.ends_at.isoformat() if study.ends_at else None,
                },
                "results": arm_results,
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        db_session.rollback()
        raise HTTPException(
            status_code=500, detail=f"Error evaluating study agents: {str(e)}"
        )
    finally:
        db_session.close()


def _pct_change(value: float, baseline: float) -> float:
    """Percentage change of ``value`` against ``baseline``.

    Returns 0.0 when the baseline is zero: with no baseline to compare against,
    a made-up ratio would read as a real effect. The absolute metrics are
    reported alongside, so a caller can see that the baseline arm had no data.
    """
    if not baseline:
        return 0.0
    return ((value - baseline) / baseline) * 100
