from typing import Type

from sqlalchemy.orm import Session
from sqlalchemy import func

from . import db_models, db_schemas
from .db_models import *
from .db_schemas import *

import re

import hashlib


# helper functions
def is_valid_uuid(uuid: str) -> bool:
    if not uuid:
        return False
    uuid = str(uuid)
    uuidv4_pattern = re.compile(
        r"\b[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-4[0-9a-fA-F]{3}\-[89aAbB][0-9a-fA-F]{3}\-[0-9a-fA-F]{12}\b"
    )
    return bool(uuidv4_pattern.fullmatch(uuid))


# Helper function to hash passwords
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# READ operation
# User Table


# Get user by email
def get_user_by_email(db: Session, email: str) -> db_models.User:
    return db.query(db_models.User).filter(db_models.User.email == email).first()


# Create new user (authentication version)
def create_auth_user(db: Session, user: db_schemas.UserCreate) -> db_models.User:
    # Hash the password
    hashed_password = hash_password(user.password)

    # Create user object
    db_user = db_models.User(
        token=user.token,
        joined_at=user.joined_at,
        email=user.email,
        name=user.name,
        password_hash=hashed_password,
        is_google_signup=user.is_google_signup,
        verified=user.verified,
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


# Verify user's password
def verify_password(db: Session, email: str, password: str) -> bool:
    user = get_user_by_email(db, email)
    if not user:
        return False

    hashed_password = hash_password(password)
    return user.password_hash == hashed_password


# Set user verification status
def set_user_verified(
    db: Session, user_token: str, verified: bool = True
) -> db_models.User:
    user = get_user_by_token(db, user_token)
    if user:
        user.verified = verified
        db.commit()
        db.refresh(user)
    return user


def get_all_users(db: Session) -> list[Type[User]]:
    return db.query(db_models.User).all()


def get_user_by_token(db: Session, token: str) -> db_models.User:
    assert is_valid_uuid(token)
    return db.query(db_models.User).filter(db_models.User.token == token).first()


# Query Table
def get_all_queries(db: Session) -> list[db_models.Query]:
    return db.query(db_models.Query).all()


def get_query_by_id(db: Session, query_id: str) -> db_models.Query:
    assert is_valid_uuid(query_id)
    return (
        db.query(db_models.Query).filter(db_models.Query.query_id == query_id).first()
    )


def get_user_queries(db: Session, user_id: str) -> list[db_models.Query]:
    assert is_valid_uuid(user_id)
    return db.query(db_models.Query).filter(db_models.Query.user_id == user_id).all()


# TODO: change this to db_models.Query
def get_queries_in_time_range(
    db: Session, start_time: str = None, end_time: str = None
) -> list[Type[Query]]:
    if start_time and end_time:
        return (
            db.query(db_models.Query)
            .filter(
                db_models.Query.timestamp >= start_time,
                db_models.Query.timestamp <= end_time,
            )
            .all()
        )
    elif start_time:
        return (
            db.query(db_models.Query)
            .filter(db_models.Query.timestamp >= start_time)
            .all()
        )
    elif end_time:
        return (
            db.query(db_models.Query)
            .filter(db_models.Query.timestamp <= end_time)
            .all()
        )
    return db.query(db_models.Query).all()


# TODO: change this to db_models.Query
def get_queries_bound_by_context(db: Session, context_id: str) -> list[Type[Query]]:
    assert is_valid_uuid(context_id)
    return (
        db.query(db_models.Query).filter(db_models.Query.context_id == context_id).all()
    )


def get_query_by_telemetry_id(db: Session, telemetry_id: str) -> db_models.Query:
    assert is_valid_uuid(telemetry_id)
    return (
        db.query(db_models.Query)
        .filter(db_models.Query.telemetry_id == telemetry_id)
        .first()
    )


# programming_language Table
def get_all_programming_languages(
    db: Session,
) -> list[Type[db_models.ProgrammingLanguage]]:
    return db.query(db_models.ProgrammingLanguage).all()


def get_programming_language_by_id(
    db: Session, language_id: int
) -> db_models.ProgrammingLanguage:
    return (
        db.query(db_models.ProgrammingLanguage)
        .filter(db_models.ProgrammingLanguage.language_id == language_id)
        .first()
    )


def get_programming_language_by_name(
    db: Session, language_name: str
) -> db_models.ProgrammingLanguage:
    return (
        db.query(db_models.ProgrammingLanguage)
        .filter(db_models.ProgrammingLanguage.language_name == language_name)
        .first()
    )


# had_generation Table
def get_all_generations(db: Session) -> list[Type[db_models.HadGeneration]]:
    return db.query(db_models.HadGeneration).all()


def get_generations_by_query_id(
    db: Session, query_id: str
) -> list[Type[db_models.HadGeneration]]:
    assert is_valid_uuid(query_id)
    return (
        db.query(db_models.HadGeneration)
        .filter(db_models.HadGeneration.query_id == query_id)
        .all()
    )


def get_generations_by_query_and_model_id(
    db: Session, query_id: str, model_id: int
) -> Type[db_models.HadGeneration]:
    assert is_valid_uuid(query_id)
    return (
        db.query(db_models.HadGeneration)
        .filter(
            db_models.HadGeneration.query_id == query_id,
            db_models.HadGeneration.model_id == model_id,
        )
        .first()
    )


def get_generations_having_confidence_in_range(
    db: Session, lower_bound: float = None, upper_bound: float = None
) -> list[Type[db_models.HadGeneration]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(
                db_models.HadGeneration.confidence >= lower_bound,
                db_models.HadGeneration.confidence <= upper_bound,
            )
            .all()
        )
    elif lower_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(db_models.HadGeneration.confidence >= lower_bound)
            .all()
        )
    elif upper_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(db_models.HadGeneration.confidence <= upper_bound)
            .all()
        )

    return get_all_generations(db)


def get_generations_having_acceptance_of(
    db: Session, acceptance: bool
) -> list[Type[db_models.HadGeneration]]:
    return (
        db.query(db_models.HadGeneration)
        .filter(db_models.HadGeneration.was_accepted == acceptance)
        .all()
    )


def get_generations_with_shown_times_in_range(
    db: Session, lower_bound: int = None, upper_bound: int = None
) -> list[Type[db_models.HadGeneration]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(
                func.cardinality(db_models.HadGeneration.shown_at) >= lower_bound,
                func.cardinality(db_models.HadGeneration.shown_at) <= upper_bound,
            )
            .all()
        )

    elif lower_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(func.cardinality(db_models.HadGeneration.shown_at) >= lower_bound)
            .all()
        )

    elif upper_bound:
        return (
            db.query(db_models.HadGeneration)
            .filter(func.cardinality(db_models.HadGeneration.shown_at) <= upper_bound)
            .all()
        )

    return get_all_generations(db)


# model_name Table
def get_all_db_models(db: Session) -> list[Type[db_models.ModelName]]:
    return db.query(db_models.ModelName).all()


def get_model_by_id(db: Session, model_id: int) -> db_models.ModelName:
    return (
        db.query(db_models.ModelName)
        .filter(db_models.ModelName.model_id == model_id)
        .first()
    )


def get_model_by_name(db: Session, model_name: str) -> db_models.ModelName:
    return (
        db.query(db_models.ModelName)
        .filter(db_models.ModelName.model_name == model_name)
        .first()
    )


# ground_truth Table
def get_all_ground_truths(db: Session) -> list[Type[db_models.GroundTruth]]:
    return db.query(db_models.GroundTruth).all()


def get_ground_truths_for(
    db: Session, query_id: str
) -> list[Type[db_models.GroundTruth]]:
    assert is_valid_uuid(query_id)
    return (
        db.query(db_models.GroundTruth)
        .filter(db_models.GroundTruth.query_id == query_id)
        .all()
    )


def get_ground_truths_for_query_in_time_range(
    db: Session, query_id: str, start_time: str = None, end_time: str = None
) -> list[Type[db_models.GroundTruth]]:
    assert is_valid_uuid(query_id)
    if start_time and end_time:
        return (
            db.query(db_models.GroundTruth)
            .filter(
                db_models.GroundTruth.query_id == query_id,
                db_models.GroundTruth.truth_timestamp >= start_time,
                db_models.GroundTruth.truth_timestamp <= end_time,
            )
            .all()
        )
    elif start_time:
        return (
            db.query(db_models.GroundTruth)
            .filter(
                db_models.GroundTruth.query_id == query_id,
                db_models.GroundTruth.truth_timestamp >= start_time,
            )
            .all()
        )
    elif end_time:
        return (
            db.query(db_models.GroundTruth)
            .filter(
                db_models.GroundTruth.query_id == query_id,
                db_models.GroundTruth.truth_timestamp <= end_time,
            )
            .all()
        )

    return get_ground_truths_for(db, query_id)


# telemetry table
def get_all_telemetries(db: Session) -> list[Type[db_models.Telemetry]]:
    return db.query(db_models.Telemetry).all()


def get_telemetries_with_time_since_last_completion_in_range(
    db: Session, lower_bound: int = None, upper_bound: int = None
) -> list[Type[db_models.Telemetry]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(
                db_models.Telemetry.time_since_last_completion >= lower_bound,
                db_models.Telemetry.time_since_last_completion <= upper_bound,
            )
            .all()
        )
    elif lower_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.time_since_last_completion >= lower_bound)
            .all()
        )
    elif upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.time_since_last_completion <= upper_bound)
            .all()
        )

    return get_all_telemetries(db)


def get_telemetry_by_id(db: Session, telemetry_id: str) -> db_models.Telemetry:
    assert is_valid_uuid(telemetry_id)
    return (
        db.query(db_models.Telemetry)
        .filter(db_models.Telemetry.telemetry_id == telemetry_id)
        .first()
    )


def get_telemetries_with_typing_speed_in_range(
    db: Session, lower_bound: int = None, upper_bound: int = None
) -> list[Type[db_models.Telemetry]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(
                db_models.Telemetry.typing_speed >= lower_bound,
                db_models.Telemetry.typing_speed <= upper_bound,
            )
            .all()
        )
    elif lower_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.typing_speed >= lower_bound)
            .all()
        )
    elif upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.typing_speed <= upper_bound)
            .all()
        )

    return get_all_telemetries(db)


def get_telemetries_with_document_char_length_in_range(
    db: Session, lower_bound: int = None, upper_bound: int = None
) -> list[Type[db_models.Telemetry]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(
                db_models.Telemetry.document_char_length >= lower_bound,
                db_models.Telemetry.document_char_length <= upper_bound,
            )
            .all()
        )
    elif lower_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.document_char_length >= lower_bound)
            .all()
        )
    elif upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.document_char_length <= upper_bound)
            .all()
        )

    return get_all_telemetries(db)


def get_telemetries_with_relative_document_position_in_range(
    db: Session, lower_bound: float = None, upper_bound: float = None
) -> list[Type[db_models.Telemetry]]:
    if lower_bound and upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(
                db_models.Telemetry.relative_document_position >= lower_bound,
                db_models.Telemetry.relative_document_position <= upper_bound,
            )
            .all()
        )
    elif lower_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.relative_document_position >= lower_bound)
            .all()
        )
    elif upper_bound:
        return (
            db.query(db_models.Telemetry)
            .filter(db_models.Telemetry.relative_document_position <= upper_bound)
            .all()
        )

    return get_all_telemetries(db)


# context Table
def get_all_contexts(db: Session) -> list[Type[db_models.Context]]:
    return db.query(db_models.Context).all()


def get_context_by_id(db: Session, context_id: str) -> db_models.Context:
    assert is_valid_uuid(context_id)
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.context_id == context_id)
        .first()
    )


def get_contexts_where_language_is(
    db: Session, language_id: int
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.language_id == language_id)
        .all()
    )


def get_contexts_where_trigger_type_is(
    db: Session, trigger_type_id: int
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.trigger_type_id == trigger_type_id)
        .all()
    )


def get_contexts_where_version_is(
    db: Session, version_id: int
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.version_id == version_id)
        .all()
    )


def get_contexts_where_prefix_contains(
    db: Session, text: str
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.prefix.contains(text))
        .all()
    )


def get_contexts_where_suffix_contains(
    db: Session, text: str
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(db_models.Context.suffix.contains(text))
        .all()
    )


def get_contexts_where_prefix_or_suffix_contains(
    db: Session, text: str
) -> list[Type[db_models.Context]]:
    return (
        db.query(db_models.Context)
        .filter(
            db_models.Context.prefix.contains(text),
            db_models.Context.suffix.contains(text),
        )
        .all()
    )


# trigger_type Table
def get_all_trigger_types(db: Session) -> list[Type[db_models.TriggerType]]:
    return db.query(db_models.TriggerType).all()


def get_trigger_type_by_id(db: Session, trigger_type_id: int) -> db_models.TriggerType:
    return (
        db.query(db_models.TriggerType)
        .filter(db_models.TriggerType.trigger_type_id == trigger_type_id)
        .first()
    )


def get_trigger_type_by_name(
    db: Session, trigger_type_name: str
) -> db_models.TriggerType:
    return (
        db.query(db_models.TriggerType)
        .filter(db_models.TriggerType.trigger_type_name == trigger_type_name)
        .first()
    )


# plugin_version Table
def get_all_plugin_versions(db: Session) -> list[Type[db_models.PluginVersion]]:
    return db.query(db_models.PluginVersion).all()


def get_plugin_version_by_id(db: Session, version_id: int) -> db_models.PluginVersion:
    return (
        db.query(db_models.PluginVersion)
        .filter(db_models.PluginVersion.version_id == version_id)
        .first()
    )


def get_plugin_versions_by_ide_type(
    db: Session, ide_type: str
) -> list[Type[db_models.PluginVersion]]:
    return (
        db.query(db_models.PluginVersion)
        .filter(db_models.PluginVersion.ide_type == ide_type)
        .all()
    )


def get_plugin_versions_by_name_containing(
    db: Session, version_name: str
) -> list[Type[db_models.PluginVersion]]:
    return (
        db.query(db_models.PluginVersion)
        .filter(db_models.PluginVersion.version_name.contains(version_name))
        .all()
    )


def get_plugin_versions_by_description_containing(
    db: Session, description: str
) -> list[Type[db_models.PluginVersion]]:
    return (
        db.query(db_models.PluginVersion)
        .filter(db_models.PluginVersion.description.contains(description))
        .all()
    )


# CREATE operations
# Simple CREATE operations with complete data -> nothing has to be checked or generated/calculated before creating
def create_user(db: Session, user: db_schemas.UserCreate) -> db_models.User:
    db_user = db_models.User(**user.model_dump())
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user


def add_programming_language(
    db: Session, language: db_schemas.ProgrammingLanguageCreate
) -> db_models.ProgrammingLanguage:
    db_language = db_models.ProgrammingLanguage(**language.model_dump())
    db.add(db_language)
    db.commit()
    db.refresh(db_language)
    return db_language


def add_model(db: Session, model: db_schemas.ModelNameCreate) -> db_models.ModelName:
    db_model = db_models.ModelName(**model.model_dump())
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


def add_trigger_type(
    db: Session, trigger_type: db_schemas.TriggerTypeCreate
) -> db_models.TriggerType:
    db_trigger_type = db_models.TriggerType(**trigger_type.model_dump())
    db.add(db_trigger_type)
    db.commit()
    db.refresh(db_trigger_type)
    return db_trigger_type


def add_plugin_version(
    db: Session, version: db_schemas.PluginVersionCreate
) -> db_models.PluginVersion:
    db_version = db_models.PluginVersion(**version.model_dump())
    db.add(db_version)
    db.commit()
    db.refresh(db_version)
    return db_version


def add_context(db: Session, context: db_schemas.ContextCreate) -> db_models.Context:
    db_context = db_models.Context(**context.model_dump())
    db.add(db_context)
    db.commit()
    db.refresh(db_context)
    return db_context


def add_query(db: Session, query: db_schemas.QueryCreate) -> db_models.Query:
    db_query = db_models.Query(**query.model_dump())
    db.add(db_query)
    db.commit()
    db.refresh(db_query)
    return db_query


def add_telemetry(
    db: Session, telemetry: db_schemas.TelemetryCreate
) -> db_models.Telemetry:
    db_telemetry = db_models.Telemetry(**telemetry.model_dump())
    db.add(db_telemetry)
    db.commit()
    db.refresh(db_telemetry)
    return db_telemetry


def add_ground_truth(
    db: Session, ground_truth: db_schemas.GroundTruthCreate
) -> db_models.GroundTruth:
    db_ground_truth = db_models.GroundTruth(**ground_truth.model_dump())
    db.add(db_ground_truth)
    db.commit()
    db.refresh(db_ground_truth)
    return db_ground_truth


def add_generation(
    db: Session, generation: db_schemas.HadGenerationCreate
) -> db_models.HadGeneration:
    db_generation = db_models.HadGeneration(**generation.model_dump())
    db.add(db_generation)
    db.commit()
    db.refresh(db_generation)
    return db_generation


# UPDATE operations
def update_status_of_generation(
    db: Session,
    query_id: str,
    model_id: int,
    new_status: db_schemas.HadGenerationUpdate,
) -> bool:
    assert is_valid_uuid(query_id)
    db_generation = (
        db.query(db_models.HadGeneration)
        .filter(
            db_models.HadGeneration.query_id == query_id,
            db_models.HadGeneration.model_id == model_id,
        )
        .first()
    )
    try:
        if db_generation:
            for key, value in new_status.model_dump().items():
                setattr(db_generation, key, value)
            db.commit()
            return True
    except Exception as e:
        print(f"Error while updating generation status: {e}")
        return False
    return False
