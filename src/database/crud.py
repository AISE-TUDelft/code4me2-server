import uuid
from datetime import datetime
from typing import Optional, Type, Union

from sqlalchemy.orm import Session

import Queries as Queries
from database import db_schemas
from database.utils import hash_password, verify_password


# User
def create_user(
    db: Session, user: Union[Queries.CreateUser, Queries.CreateUserOauth]
) -> db_schemas.User:
    # Create user object
    db_user = db_schemas.User(
        user_id=uuid.uuid4(),
        joined_at=datetime.now().isoformat(),
        email=str(user.email),
        name=user.name,
        password=hash_password(user.password.get_secret_value()),
        # configure default preference and config upon creation
        config_id=user.config_id,
        preference=user.preference,
        is_oauth_signup=isinstance(user, Queries.CreateUserOauth),
        verified=False,
    )

    db.add(db_user)
    db.commit()
    return db_user


def get_user_by_id(db: Session, user_id: uuid.UUID) -> Optional[db_schemas.User]:
    return db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).first()


def get_user_by_email(db: Session, email: str) -> Optional[Type[db_schemas.User]]:
    return db.query(db_schemas.User).filter(db_schemas.User.email == email).first()


def get_user_by_email_password(
    db: Session, email: str, password: str
) -> Optional[db_schemas.User]:
    user = db.query(db_schemas.User).filter(db_schemas.User.email == email).first()
    if user and verify_password(str(user.password), password):
        return user
    return None


def get_user_by_id_password(db: Session, user_id: uuid.UUID, password: str):
    user = db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).first()
    if user and verify_password(str(user.password), password):
        return user
    return None


def update_user(
    db: Session, user_id: uuid.UUID, user_to_update: Queries.UpdateUser
) -> db_schemas.User:
    # Get all the data and manually filter out None values
    update_data = {
        key: value
        for key, value in user_to_update.dict().items()
        if value is not None and key != "previous_password"
    }
    if update_data.get("password"):
        update_data["password"] = hash_password(update_data["password"])

    if not update_data:
        # No fields to update, just return the existing user
        return (
            db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).first()
        )

    db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).update(
        update_data  # type: ignore
    )
    db.commit()
    return db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).first()


def delete_user_by_id(db: Session, user_id: uuid.UUID) -> bool:
    result = (
        db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).delete()
    )
    db.commit()
    return result > 0


# def update_user(db: Session, user_id: uuid.UUID, user_update: Queries.UpdateUser) -> Optional[db_schemas.User]:
#     user = get_user_by_id(db, user_id)
#     if user:
#         update_data = user_update.dict(exclude_unset=True)
#         for field, value in update_data.items():
#             setattr(user, field, value)
#         db.commit()
#         db.refresh(user)
#     return user


# Context Operations

# def add_context(db: Session, context: Queries.ContextData) -> db_schemas.Context:
#     """Create a new context record"""
#     db_context = db_schemas.Context(
#         context_id=uuid.uuid4(),
#         prefix=context.prefix,
#         suffix=context.suffix,
#         language_id=context.language_id,
#         trigger_type_id=context.trigger_type_id,
#         version_id=context.version_id,
#     )
#     db.add(db_context)
#     db.commit()
#     db.refresh(db_context)
#     return db_context


def create_context(
    db: Session, context: Queries.ContextData, context_id: str = ""
) -> db_schemas.Context:
    db_context = db_schemas.Context(
        context_id=uuid.uuid4() if context_id == "" else uuid.UUID(context_id),
        prefix=context.prefix,
        suffix=context.suffix,
        file_name=context.file_name,
        selected_text=context.selected_text,
    )
    db.add(db_context)
    db.commit()
    db.refresh(db_context)
    return db_context


def get_context_by_id(
    db: Session, context_id: uuid.UUID
) -> Optional[db_schemas.Context]:
    return (
        db.query(db_schemas.Context)
        .filter(db_schemas.Context.context_id == context_id)
        .first()
    )


# Telemetry operations
# def add_telemetry(
#     db: Session, telemetry: Queries.TelemetryData
# ) -> db_schemas.Telemetry:
#     """Create a new telemetry record"""
#     db_telemetry = db_schemas.Telemetry(
#         telemetry_id=uuid.uuid4(),
#         time_since_last_completion=telemetry.time_since_last_completion,
#         typing_speed=telemetry.typing_speed,
#         document_char_length=telemetry.document_char_length,
#         relative_document_position=telemetry.relative_document_position,
#     )
#     db.add(db_telemetry)
#     db.commit()
#     db.refresh(db_telemetry)
#     return db_telemetry


def create_contextual_telemetry(
    db: Session, telemetry: Queries.ContextualTelemetryData, id: str = ""
) -> db_schemas.ContextualTelemetry:
    db_telemetry = db_schemas.ContextualTelemetry(
        contextual_telemetry_id=uuid.uuid4() if id == "" else uuid.UUID(id),
        version_id=telemetry.version_id,
        trigger_type_id=telemetry.trigger_type_id,
        language_id=telemetry.language_id,
        file_path=telemetry.file_path,
        caret_line=telemetry.caret_line,
        document_char_length=telemetry.document_char_length,
        relative_document_position=telemetry.relative_document_position,
    )
    db.add(db_telemetry)
    db.commit()
    # db.refresh(db_telemetry)
    return db_telemetry


def create_behavioral_telemetry(
    db: Session, telemetry: Queries.BehavioralTelemetryData, id: str = ""
) -> db_schemas.BehavioralTelemetry:
    db_telemetry = db_schemas.BehavioralTelemetry(
        behavioral_telemetry_id=uuid.uuid4() if id == "" else uuid.UUID(id),
        time_since_last_shown=telemetry.time_since_last_shown,
        time_since_last_accepted=telemetry.time_since_last_accepted,
        typing_speed=telemetry.typing_speed,
    )
    db.add(db_telemetry)
    db.commit()
    db.refresh(db_telemetry)
    return db_telemetry


def get_contextual_telemetry_by_id(
    db: Session, telemetry_id: uuid.UUID
) -> Optional[db_schemas.ContextualTelemetry]:
    return (
        db.query(db_schemas.ContextualTelemetry)
        .filter(db_schemas.ContextualTelemetry.contextual_telemetry_id == telemetry_id)
        .first()
    )


def get_behavioral_telemetry_by_id(
    db: Session, telemetry_id: uuid.UUID
) -> Optional[db_schemas.BehavioralTelemetry]:
    return (
        db.query(db_schemas.BehavioralTelemetry)
        .filter(db_schemas.BehavioralTelemetry.behavioral_telemetry_id == telemetry_id)
        .first()
    )


# Query operations
# def add_query(db: Session, query: Queries.CreateQuery) -> db_schemas.Query:
#     """Create a new query record"""
#     db_query = db_schemas.Query(
#         query_id=uuid.uuid4(),
#         user_id=str(query.user_id),
#         telemetry_id=str(query.telemetry_id),
#         context_id=str(query.context_id),
#         timestamp=datetime.now().isoformat(),
#         total_serving_time=query.total_serving_time,
#         server_version_id=query.server_version_id,
#     )
#     db.add(db_query)
#     db.commit()
#     db.refresh(db_query)
#     return db_query
#
#
# def get_query_by_id(db: Session, query_id: str) -> Optional[Type[db_schemas.Query]]:
#     """Get query by ID"""
#     return (
#         db.query(db_schemas.Query).filter(db_schemas.Query.query_id == query_id).first()
#     )
#
#
# def update_query_serving_time(
#     db: Session, query_id: str, total_serving_time: int
# ) -> Optional[db_schemas.Query]:
#     """Update query total serving time"""
#     query = get_query_by_id(db, query_id)
#     if query:
#         query.total_serving_time = total_serving_time
#         db.commit()
#         db.refresh(query)
#     return query
#
#
# def remove_query_by_user_id(db: Session, user_id: str) -> bool:
#     """Remove all queries by user ID"""
#     result = (
#         db.query(db_schemas.Query).filter(db_schemas.Query.user_id == user_id).delete()
#     )
#     db.commit()
#     return result > 0


# MetaQuery operations
def create_completion_query(
    db: Session, query: Queries.CreateCompletionQuery, id: str = ""
) -> db_schemas.CompletionQuery:
    # Create the completion query directly using joined table inheritance
    # This will automatically create both the meta query and completion_query records
    db_meta_query = db_schemas.MetaQuery(
        meta_query_id=uuid.uuid4() if id == "" else uuid.UUID(id),
        user_id=query.user_id,
        contextual_telemetry_id=query.contextual_telemetry_id,
        behavioral_telemetry_id=query.behavioral_telemetry_id,
        context_id=query.context_id,
        session_id=query.session_id,
        project_id=query.project_id,
        multi_file_context_changes_indexes=query.multi_file_context_changes_indexes,
        timestamp=datetime.now(),
        total_serving_time=query.total_serving_time,
        server_version_id=query.server_version_id,
        query_type="completion",
    )

    db_completion_query = db_schemas.CompletionQuery(
        meta_query_id=db_meta_query.meta_query_id
    )

    # Set the fields specific to CompletionQuery
    db.add(db_meta_query)
    db.commit()
    db.add(db_completion_query)
    db.commit()
    db.refresh(db_meta_query)
    db.refresh(db_completion_query)
    return db_completion_query


def create_chat_query(
    db: Session, query: Queries.CreateChatQuery
) -> db_schemas.ChatQuery:
    meta_query_id = uuid.uuid4()

    # Create the chat query directly using joined table inheritance
    # This will automatically create both the meta_query and chat_query records
    db_chat_query = db_schemas.ChatQuery(
        meta_query_id=meta_query_id,
        user_id=query.user_id,
        contextual_telemetry_id=query.contextual_telemetry_id,
        behavioral_telemetry_id=query.behavioral_telemetry_id,
        context_id=query.context_id,
        session_id=query.session_id,
        project_id=query.project_id,
        multi_file_context_changes_indexes=query.multi_file_context_changes_indexes,
        timestamp=datetime.now(),
        total_serving_time=query.total_serving_time,
        server_version_id=query.server_version_id,
        query_type="chat",
        chat_id=query.chat_id,
        web_enabled=query.web_enabled,
    )

    db.add(db_chat_query)
    db.commit()
    db.refresh(db_chat_query)
    return db_chat_query


def get_meta_query_by_id(
    db: Session, meta_query_id: uuid.UUID
) -> Optional[db_schemas.MetaQuery]:
    return (
        db.query(db_schemas.MetaQuery)
        .filter(db_schemas.MetaQuery.meta_query_id == meta_query_id)
        .first()
    )


def get_completion_query_by_id(
    db: Session, meta_query_id: uuid.UUID
) -> Optional[db_schemas.CompletionQuery]:
    return (
        db.query(db_schemas.CompletionQuery)
        .filter(db_schemas.CompletionQuery.meta_query_id == meta_query_id)
        .first()
    )


def get_chat_query_by_id(
    db: Session, meta_query_id: uuid.UUID
) -> Optional[db_schemas.ChatQuery]:
    return (
        db.query(db_schemas.ChatQuery)
        .filter(db_schemas.ChatQuery.meta_query_id == meta_query_id)
        .first()
    )


def get_chat_queries_for_chat(
    db: Session, chat_id: uuid.UUID
) -> list[db_schemas.ChatQuery]:
    return (
        db.query(db_schemas.ChatQuery)
        .filter(db_schemas.ChatQuery.chat_id == chat_id)
        .all()
    )


def delete_meta_query_cascade(db: Session, meta_query_id: uuid.UUID) -> bool:
    """
    Properly delete meta_query with all cascading relationships
    """
    meta_query = get_meta_query_by_id(db, meta_query_id)
    if not meta_query:
        return False

    try:
        db.delete(meta_query)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e


# Generation operations

# def add_generation(
#     db: Session, generation: Queries.CreateGeneration
# ) -> db_schemas.HadGeneration:
#     """Create a new generation record"""
#     db_generation = db_schemas.HadGeneration(
#         query_id=str(generation.query_id),
#         model_id=generation.model_id,
#         completion=generation.completion,
#         generation_time=generation.generation_time,
#         shown_at=generation.shown_at,
#         was_accepted=generation.was_accepted,
#         confidence=generation.confidence,
#         logprobs=generation.logprobs,
#     )
#     db.add(db_generation)
#     db.commit()
#     db.refresh(db_generation)
#     return db_generation
# def get_generations_by_query_id(
#     db: Session, query_id: str
# ) -> list[db_schemas.HadGeneration]:
#     """Get all generations for a query"""
#     # assert is_valid_uuid(query_id)
#     return (
#         db.query(db_schemas.HadGeneration)
#         .filter(db_schemas.HadGeneration.query_id == query_id)
#         .all()
#     )
# def get_generations_by_query_and_model_id(
#     db: Session, query_id: str, model_id: int
# ) -> Optional[db_schemas.HadGeneration]:
#     """Get generation by query ID and model ID"""
#     return (
#         db.query(db_schemas.HadGeneration)
#         .filter(
#             db_schemas.HadGeneration.query_id == query_id,
#             db_schemas.HadGeneration.model_id == model_id,
#         )
#         .first()
#     )


def create_generation(
    db: Session, generation: Queries.CreateGeneration
) -> db_schemas.HadGeneration:
    # Convert string timestamps to datetime objects
    shown_at_datetimes = [datetime.fromisoformat(ts) for ts in generation.shown_at]

    db_generation = db_schemas.HadGeneration(
        meta_query_id=generation.meta_query_id,
        model_id=generation.model_id,
        completion=generation.completion,
        generation_time=generation.generation_time,
        shown_at=shown_at_datetimes,
        was_accepted=generation.was_accepted,
        confidence=generation.confidence,
        logprobs=generation.logprobs,
    )
    db.add(db_generation)
    db.commit()
    # db.refresh(db_generation)
    return db_generation


def get_generations_by_meta_query(
    db: Session, meta_query_id: uuid.UUID
) -> list[db_schemas.HadGeneration]:
    return (
        db.query(db_schemas.HadGeneration)
        .filter(db_schemas.HadGeneration.meta_query_id == meta_query_id)
        .all()
    )


def update_generation_acceptance(
    db: Session, update_data: Queries.UpdateGenerationAcceptance
) -> Optional[db_schemas.HadGeneration]:
    """Update generation acceptance status"""
    generation = get_generation_by_meta_query_and_model(
        db, update_data.meta_query_id, update_data.model_id
    )
    if generation:
        setattr(generation, "was_accepted", update_data.was_accepted)
        db.commit()
        db.refresh(generation)
    return generation


def get_generation_by_meta_query_and_model(
    db: Session, meta_query_id: uuid.UUID, model_id: int
) -> Optional[db_schemas.HadGeneration]:
    return (
        db.query(db_schemas.HadGeneration)
        .filter(
            db_schemas.HadGeneration.meta_query_id == meta_query_id,
            db_schemas.HadGeneration.model_id == model_id,
        )
        .first()
    )


def get_generations_by_meta_query_id(
    db: Session, meta_query_id: str
) -> list[db_schemas.HadGeneration]:
    return (
        db.query(db_schemas.HadGeneration)
        .filter(db_schemas.HadGeneration.meta_query_id == uuid.UUID(meta_query_id))
        .all()
    )


# Model operations
def create_model(db: Session, model: Queries.CreateModel) -> db_schemas.ModelName:
    db_model = db_schemas.ModelName(
        model_name=model.model_name,
        is_instruction_tuned=model.is_instruction_tuned,
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


def update_generation(
    db: Session, query_id: str, model_id: int, generation: Queries.UpdateGeneration
):
    """Update an existing generation"""
    result = (
        db.query(db_schemas.HadGeneration)
        .filter(
            db_schemas.HadGeneration.query_id == query_id,
            db_schemas.HadGeneration.model_id == model_id,
        )
        .update(**generation.dict())
    )
    db.commit()
    return result > 0


def get_model_by_id(db: Session, model_id: int) -> Optional[db_schemas.ModelName]:
    """Get model by ID"""
    return (
        db.query(db_schemas.ModelName)
        .filter(db_schemas.ModelName.model_id == model_id)
        .first()
    )


def get_all_model_names(db: Session) -> list[db_schemas.ModelName]:
    return db.query(db_schemas.ModelName).all()


def get_all_models(db: Session) -> list[db_schemas.ModelName]:
    return db.query(db_schemas.ModelName).all()


# Chat operations
def create_chat(db: Session, chat: Queries.CreateChat) -> db_schemas.Chat:
    db_chat = db_schemas.Chat(
        chat_id=uuid.uuid4(),
        project_id=chat.project_id,
        user_id=chat.user_id,
        title=chat.title,
        created_at=datetime.now(),
    )
    db.add(db_chat)
    db.commit()
    db.refresh(db_chat)
    return db_chat


def get_chat_by_id(db: Session, chat_id: uuid.UUID) -> Optional[db_schemas.Chat]:
    return db.query(db_schemas.Chat).filter(db_schemas.Chat.chat_id == chat_id).first()


def update_chat(
    db: Session, chat_id: uuid.UUID, chat_update: Queries.UpdateChat
) -> Optional[db_schemas.Chat]:
    chat = get_chat_by_id(db, chat_id)
    if chat:
        update_data = chat_update.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(chat, field, value)
        db.commit()
        db.refresh(chat)
    return chat


def get_chats_for_project(db: Session, project_id: uuid.UUID) -> list[db_schemas.Chat]:
    return (
        db.query(db_schemas.Chat).filter(db_schemas.Chat.project_id == project_id).all()
    )


def get_chats_for_user(db: Session, user_id: uuid.UUID) -> list[db_schemas.Chat]:
    return db.query(db_schemas.Chat).filter(db_schemas.Chat.user_id == user_id).all()


def delete_chat_cascade(db: Session, chat_id: uuid.UUID) -> bool:
    """
    Properly delete chat with all cascading relationships
    """
    chat = get_chat_by_id(db, chat_id)
    if not chat:
        return False

    try:
        db.delete(chat)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e


# Config Operations
def get_config_by_id(db: Session, config_id: int) -> Optional[db_schemas.Config]:
    return (
        db.query(db_schemas.Config)
        .filter(db_schemas.Config.config_id == config_id)
        .first()
    )


def create_config(db: Session, config: Queries.CreateConfig) -> db_schemas.Config:
    db_config = db_schemas.Config(config_data=config.config_data)
    db.add(db_config)
    db.commit()
    db.refresh(db_config)
    return db_config


def get_all_configs(db: Session) -> list[db_schemas.Config]:
    return db.query(db_schemas.Config).all()


# Project Operations
def create_project(db: Session, project: Queries.CreateProject) -> db_schemas.Project:
    db_project = db_schemas.Project(
        project_id=uuid.uuid4(),
        project_name=project.project_name,
        multi_file_contexts=project.multi_file_contexts,
        multi_file_context_changes=project.multi_file_context_changes,
        created_at=datetime.now(),
    )
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project


def get_project_by_id(
    db: Session, project_id: uuid.UUID
) -> Optional[db_schemas.Project]:
    return (
        db.query(db_schemas.Project)
        .filter(db_schemas.Project.project_id == project_id)
        .first()
    )


def update_project(
    db: Session, project_id: uuid.UUID, project_update: Queries.UpdateProject
) -> Optional[db_schemas.Project]:
    project = get_project_by_id(db, project_id)
    if project:
        update_data = project_update.dict(exclude_unset=True)
        for field, value in update_data.items():
            setattr(project, field, value)
        db.commit()
        db.refresh(project)
    return project


def get_projects_for_user(db: Session, user_id: uuid.UUID) -> list[db_schemas.Project]:
    return (
        db.query(db_schemas.Project)
        .join(db_schemas.ProjectUser)
        .filter(db_schemas.ProjectUser.user_id == user_id)
        .all()
    )


def add_user_to_project(
    db: Session, project_user: Queries.AddUserToProject
) -> db_schemas.ProjectUser:
    db_project_user = db_schemas.ProjectUser(
        project_id=project_user.project_id,
        user_id=project_user.user_id,
        # role=project_user.role,
        joined_at=datetime.now(),
    )
    db.add(db_project_user)
    db.commit()
    db.refresh(db_project_user)
    return db_project_user


def remove_user_from_project(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    result = (
        db.query(db_schemas.ProjectUser)
        .filter(
            db_schemas.ProjectUser.project_id == project_id,
            db_schemas.ProjectUser.user_id == user_id,
        )
        .delete()
    )
    db.commit()
    return result > 0


def get_project_users(
    db: Session, project_id: uuid.UUID
) -> list[db_schemas.ProjectUser]:
    return (
        db.query(db_schemas.ProjectUser)
        .filter(db_schemas.ProjectUser.project_id == project_id)
        .all()
    )


def delete_project_cascade(db: Session, project_id: uuid.UUID) -> bool:
    """
    Properly delete project with all cascading relationships
    """
    project = get_project_by_id(db, project_id)
    if not project:
        return False

    try:
        db.delete(project)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e


# New Session Operations
def create_session(
    db: Session, session: Queries.CreateSession, id: str = ""
) -> db_schemas.Session:
    db_session = db_schemas.Session(
        session_id=uuid.uuid4() if id == "" else uuid.UUID(id),
        user_id=session.user_id,
        start_time=datetime.now().isoformat(),
        end_time=None,
    )
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    return db_session


def create_session_project(
    db: Session, session_project: Queries.CreateSessionProject
) -> db_schemas.SessionProject:
    db_session_project = db_schemas.SessionProject(
        session_id=session_project.session_id, project_id=session_project.project_id
    )

    db.add(db_session_project)
    db.commit()
    return db_session_project


def update_session(
    db: Session,
    session_id: uuid.UUID,
    session_update: Queries.UpdateSession,
) -> Optional[db_schemas.Session]:
    session = get_session_by_id(db, session_id)
    if session and session_update.end_time:
        setattr(session, "end_time", datetime.fromisoformat(session_update.end_time))
        db.commit()
        # db.refresh(session)
    return session


def get_sessions_for_user(db: Session, user_id: uuid.UUID) -> list[db_schemas.Session]:
    return (
        db.query(db_schemas.Session).filter(db_schemas.Session.user_id == user_id).all()
    )


def get_session_by_id(
    db: Session, session_id: uuid.UUID
) -> Optional[db_schemas.Session]:
    return (
        db.query(db_schemas.Session)
        .filter(db_schemas.Session.session_id == session_id)
        .first()
    )


def delete_session_cascade(db: Session, session_id: uuid.UUID) -> bool:
    """
    Properly delete session with all cascading relationships
    """
    session = get_session_by_id(db, session_id)
    if not session:
        return False

    try:
        db.delete(session)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        raise e


def create_ground_truth(
    db: Session, ground_truth: Queries.CreateGroundTruth
) -> db_schemas.GroundTruth:
    """Create a ground truth record"""
    db_ground_truth = db_schemas.GroundTruth(
        completion_query_id=ground_truth.completion_query_id,
        truth_timestamp=datetime.now(),
        ground_truth=ground_truth.ground_truth,
    )
    db.add(db_ground_truth)
    db.commit()
    db.refresh(db_ground_truth)
    return db_ground_truth


def get_ground_truths_for_completion(
    db: Session, completion_query_id: uuid.UUID
) -> list[db_schemas.GroundTruth]:
    return (
        db.query(db_schemas.GroundTruth)
        .filter(db_schemas.GroundTruth.completion_query_id == completion_query_id)
        .all()
    )


def get_all_programming_languages(db: Session) -> list[db_schemas.ProgrammingLanguage]:
    return db.query(db_schemas.ProgrammingLanguage).all()


def get_all_trigger_types(db: Session) -> list[db_schemas.TriggerType]:
    return db.query(db_schemas.TriggerType).all()


def get_all_plugin_versions(db: Session) -> list[db_schemas.PluginVersion]:
    return db.query(db_schemas.PluginVersion).all()


def get_programming_language_by_id(
    db: Session, language_id: int
) -> Optional[db_schemas.ProgrammingLanguage]:
    return (
        db.query(db_schemas.ProgrammingLanguage)
        .filter(db_schemas.ProgrammingLanguage.language_id == language_id)
        .first()
    )


def get_trigger_type_by_id(
    db: Session, trigger_type_id: int
) -> Optional[db_schemas.TriggerType]:
    return (
        db.query(db_schemas.TriggerType)
        .filter(db_schemas.TriggerType.trigger_type_id == trigger_type_id)
        .first()
    )


def get_plugin_version_by_id(
    db: Session, version_id: int
) -> Optional[db_schemas.PluginVersion]:
    return (
        db.query(db_schemas.PluginVersion)
        .filter(db_schemas.PluginVersion.version_id == version_id)
        .first()
    )


# Old Sessions
# Kept in case we need to revert to the old session management
# def get_session_by_id(db: Session, session_id: str) -> Optional[db_schemas.Session]:
#     return (
#         db.query(db_schemas.Session)
#         .filter(db_schemas.Session.session_id == session_id)
#         .first()
#     )
#
#
# def delete_session_by_id(db: Session, session_id: str) -> None:
#     db.query(db_schemas.Session).filter(
#         db_schemas.Session.session_id == session_id
#     ).delete()
#     db.commit()
#
#
# def add_session(db: Session, session: db_schemas.Session) -> None:
#     db.add(session)
#     db.commit()
#     db.refresh(session)
#
# def remove_session_by_user_id(db: Session, user_id: str) -> bool:
#     """Remove all sessions by user ID"""
#     result = (
#         db.query(db_schemas.Session)
#         .filter(db_schemas.Session.user_id == user_id)
#         .delete()
#     )
#     db.commit()
#     return result > 0
#
#
# def add_session_query(db: Session, session_query: db_schemas.SessionQuery) -> None:
#     logging.log(logging.INFO, "Add session query is called")
#     db.add(session_query)
#     db.commit()
#     db.refresh(session_query)
#
#
# def create_session(db: Session, user_id: str) -> db_schemas.Session:
#     session = db_schemas.Session(session_id=uuid.uuid4(), user_id=user_id)
#     db.add(session)
#     db.commit()
#     db.refresh(session)
#     return session
