# TODO: reformat
"""
Celery tasks for database operations in Code4meV2.

This module contains asynchronous Celery tasks for creating and updating various
database entities including contexts, telemetry data, queries, generations, and
ground truth records. All tasks use database transactions with automatic rollback
on errors and follow a consistent pattern for data validation and persistence.
"""

import Queries
from App import App
from backend.email_utils import send_reset_password_email, send_verification_email
from celery_app.celery_app import celery
from database import crud
from utils import create_uuid


@celery.task
def add_context_task(context_data: dict, context_id: str = None):  # type: ignore
    """
    Create a new context record in the database.

    Args:
        context_data: Dictionary containing context data to be stored
        context_id: Optional custom ID for the context record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            context_query = Queries.ContextData(**context_data)
            crud.create_context(db=db, context=context_query, context_id=context_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_contextual_telemetry_task(
    contextual_telemetry_data: dict,
    contextual_telemetry_id: str = None,  # type: ignore
):
    """
    Create a new contextual telemetry record in the database.

    Contextual telemetry captures environment and usage context information
    that helps understand how the application is being used.

    Args:
        contextual_telemetry_data: Dictionary containing telemetry data
        contextual_telemetry_id: Optional custom ID for the telemetry record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            contextual_telemetry_query = Queries.ContextualTelemetryData(
                **contextual_telemetry_data
            )
            crud.create_contextual_telemetry(
                db=db, telemetry=contextual_telemetry_query, id=contextual_telemetry_id
            )
        except Exception:
            db.rollback()
            raise


@celery.task
def add_behavioral_telemetry_task(
    behavioral_telemetry_data: dict,
    behavioral_telemetry_id: str = None,  # type: ignore
):
    """
    Create a new behavioral telemetry record in the database.

    Behavioral telemetry captures user interaction patterns and behaviors
    to improve user experience and application performance.

    Args:
        behavioral_telemetry_data: Dictionary containing behavioral data
        behavioral_telemetry_id: Optional custom ID for the telemetry record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            behavioral_telemetry_query = Queries.BehavioralTelemetryData(
                **behavioral_telemetry_data
            )
            crud.create_behavioral_telemetry(
                db=db, telemetry=behavioral_telemetry_query, id=behavioral_telemetry_id
            )
        except Exception:
            db.rollback()
            raise


@celery.task
def add_completion_query_task(query_data: dict, query_id: str = None):  # type: ignore
    """
    Create a new completion query record in the database.

    Completion queries represent user requests for code completion suggestions
    and store the context and parameters used for generating completions.

    Args:
        query_data: Dictionary containing completion query data
        query_id: Optional custom ID for the query record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            query_query = Queries.CreateCompletionQuery(**query_data)
            crud.create_completion_query(db=db, query=query_query, id=query_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_chat_query_task(query_data: dict, query_id: str = None):  # type: ignore
    """
    Create a new chat query record in the database.

    Chat queries represent user messages in conversational interactions
    and store the message content and associated metadata.

    Args:
        query_data: Dictionary containing chat query data
        query_id: Optional custom ID for the query record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            query_query = Queries.CreateChatQuery(**query_data)
            crud.create_chat_query(db=db, query=query_query, id=query_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def get_or_create_chat_task(chat_data: dict, chat_id: str = None):  # type: ignore
    """
    Create a new chat session record in the database.

    Chat sessions represent ongoing conversations between users and the system,
    maintaining context and history across multiple message exchanges.

    Args:
        chat_data: Dictionary containing chat session data
        chat_id: Optional custom ID for the chat record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            chat_query = Queries.CreateChat(**chat_data)
            crud.create_chat(db=db, chat=chat_query, chat_id=chat_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def add_generation_task(generation_data: dict, generation_id: str = None):  # type: ignore
    """
    Create a new generation record in the database.

    Generation records store the outputs produced by AI models in response
    to user queries, including metadata about the generation process.

    Args:
        generation_data: Dictionary containing generation data
        generation_id: Optional custom ID for the generation record

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            generation_query = Queries.CreateGeneration(**generation_data)
            crud.create_generation(db=db, generation=generation_query, id=generation_id)
        except Exception:
            db.rollback()
            raise


@celery.task
def update_generation_task(query_id: str, model_id: int, generation_data: dict):
    """
    Update an existing generation record in the database.

    This task modifies generation records with new data, typically used
    to update status, add completion information, or store feedback.

    Args:
        query_id: ID of the query associated with the generation
        model_id: ID of the model that produced the generation
        generation_data: Dictionary containing updated generation data

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
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
def add_ground_truth_task(ground_truth_data: dict):
    """
    Create a new ground truth record in the database.

    Ground truth records store verified correct answers or expected outputs
    that can be used for model evaluation and training purposes.

    Args:
        ground_truth_data: Dictionary containing ground truth data

    Raises:
        Exception: Re-raises any database operation errors after rollback
    """
    with App.get_instance().get_db_session_fresh() as db:
        try:
            ground_truth_query = Queries.CreateGroundTruth(**ground_truth_data)
            crud.create_ground_truth(db=db, ground_truth=ground_truth_query)
        except Exception:
            db.rollback()
            raise


@celery.task
def send_verification_email_task(user_id: str, user_email: str, user_name: str):
    """
    Send an email verification message to a user asynchronously.

    This task generates a unique verification token, stores it in Redis
    with an expiration time, and sends the verification email to the user.
    The token can later be used to verify the user's email address.

    Args:
        user_id: Unique identifier for the user account
        user_email: Email address to send verification to
        user_name: Display name of the user for personalization
    """
    # Generate unique verification token
    verification_token = create_uuid()

    # Store token in Redis with user_id as value and automatic expiration
    app = App.get_instance()
    app.get_redis_manager().set(
        "email_verification",
        verification_token,
        {"user_id": user_id},
        force_reset_exp=True,
    )

    # Send the verification email with the generated token
    send_verification_email(user_email, user_name, verification_token)


@celery.task
def send_reset_password_email_task(user_id: str, user_email: str, user_name: str):
    """
    Send a password reset email to the user.

    This task generates a unique reset token, stores it in Redis with an expiration time,
    and sends the reset password email to the user.

    Args:
        user_id: Unique identifier for the user account
        user_email: Email address to send the reset link to
        user_name: Display name of the user for personalization
    """
    # # Generate a secure reset token
    reset_token = create_uuid()

    # # Store the reset token in Redis with user info and expiration (15 minutes)
    app = App.get_instance()
    app.get_redis_manager().set(
        "password_reset",
        reset_token,
        {"user_id": user_id, "email": user_email},
        force_reset_exp=True,
    )
    # Send the password reset email with the generated token
    send_reset_password_email(user_email, user_name, reset_token)
