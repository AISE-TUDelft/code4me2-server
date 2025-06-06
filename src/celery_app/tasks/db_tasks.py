import Queries
from App import App
from celery_app.celery_app import celery
from database import crud


@celery.task
def add_context_task(context_data: dict, context_id: str = None):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            context_query = Queries.ContextData(**context_data)
            crud.create_context(db=db, context=context_query, context_id=context_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_telemetry_task(
    contextual_telemetry_data: dict,
    behavioral_telemetry_data: dict,
    contextual_telemetry_id: str = None,
    behavioral_telemetry_id: str = None,
):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            contextual_telemetry_query = Queries.ContextualTelemetryData(
                **contextual_telemetry_data
            )
            behavioral_telemetry_query = Queries.BehavioralTelemetryData(
                **behavioral_telemetry_data
            )
            crud.create_contextual_telemetry(
                db=db, telemetry=contextual_telemetry_query, id=contextual_telemetry_id
            )
            crud.create_behavioral_telemetry(
                db=db, telemetry=behavioral_telemetry_query, id=behavioral_telemetry_id
            )
        except Exception:
            db.rollback()
            raise


@celery.task
def add_completion_query_task(query_data: dict, query_id: str = None):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            query_query = Queries.CreateCompletionQuery(**query_data)
            crud.create_completion_query(db=db, query=query_query, id=query_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_session_query_task(session_query_data: dict):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            session_query_query = Queries.SessionQueryData(**session_query_data)
            crud.add_session_query(db=db, session_query=session_query_query)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_generation_task(generation_data: dict, generation_id: str = None):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            generation_query = Queries.CreateGeneration(**generation_data)
            crud.create_generation(db=db, generation=generation_query)
        except Exception:
            db.rollback()
            raise


@celery.task
def update_generation_task(query_id: str, model_id: int, generation_data: dict):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            generation_query = Queries.UpdateGeneration(**generation_data)
            crud.update_generation(
                db=db, query_id=query_id, model_id=model_id, generation=generation_query
            )
        except Exception:
            db.rollback()
            raise


@celery.task
def update_query_task(query_id: str, query_data: dict):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            query_query = Queries.UpdateQuery(**query_data)
            crud.update_query(db=db, query_id=query_id, query=query_query)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_ground_truth_task(ground_truth_data: dict):
    with App.get_instance().get_db_session_fresh() as db:
        try:
            ground_truth_query = Queries.CreateGroundTruth(**ground_truth_data)
            crud.add_ground_truth(db=db, ground_truth=ground_truth_query)
        except Exception:
            db.rollback()
            raise
