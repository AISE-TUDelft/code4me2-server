import json
import uuid
from datetime import datetime
from typing import List, Optional, Tuple, Type, Union

from sqlalchemy import func, text
from sqlalchemy.orm import Session

import Queries as Queries
from database import db_schemas
from database.db_schemas import DEFAULT_USER_PREFERENCE
from database.embedding_service import encode_text
from utils import hash_password, verify_password


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
        preference=json.dumps(DEFAULT_USER_PREFERENCE),
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
) -> Optional[db_schemas.User]:
    # Get all the data and manually filter out None values
    update_data = user_to_update.dict(exclude_unset=True, to_json_values=True)
    update_data.pop("previous_password", None)
    if update_data.get("password"):
        update_data["password"] = hash_password(update_data["password"])
    if update_data.get("preference"):
        preference = DEFAULT_USER_PREFERENCE | json.loads(update_data["preference"])
        update_data["preference"] = json.dumps(preference)
    result = (
        db.query(db_schemas.User)
        .filter(db_schemas.User.user_id == user_id)
        .update(update_data)  # type: ignore
    )
    db.commit()
    if result:
        return get_user_by_id(db, user_id)
    return None


def delete_user_by_id(db: Session, user_id: uuid.UUID) -> bool:
    result = (
        db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).delete()
    )
    db.commit()
    return result > 0


def delete_user_full_wipe_out(db: Session, user_id: uuid.UUID):
    meta_queries = (
        db.query(db_schemas.MetaQuery)
        .filter(db_schemas.MetaQuery.user_id == user_id)
        .all()
    )
    db.query(db_schemas.MetaQuery).filter(
        db_schemas.MetaQuery.user_id == user_id
    ).delete()
    project_users = (
        db.query(db_schemas.ProjectUser)
        .filter(db_schemas.ProjectUser.user_id == user_id)
        .all()
    )
    # TODO: Is not fully tested yet because in current settings the frontend doesn't support multi user project edit
    for project_user in project_users:
        project_context_should_be_deleted = True
        project_should_be_deleted = True
        common_project_users = (
            db.query(db_schemas.ProjectUser)
            .filter(db_schemas.ProjectUser.project_id == project_user.project_id)
            .all()
        )
        # Check if there exists another user working on this project don't delete this project as a whole
        if len(common_project_users) > 1:
            project_should_be_deleted = False
        # Check if there exists a user who has agreed to store context on the same project keep the context
        for common_project_user in common_project_users:
            common_user = (
                db.query(db_schemas.User)
                .filter(db_schemas.User.user_id == common_project_user.user_id)
                .first()
            )
            if common_user and json.loads(common_user.preference).get(
                "store_context", False
            ):
                project_context_should_be_deleted = False
                break
        if project_should_be_deleted:
            db.query(db_schemas.Project).filter(
                db_schemas.Project.project_id == project_user.project_id
            ).delete()
        elif project_context_should_be_deleted:
            db.query(db_schemas.Project).filter(
                db_schemas.Project.project_id == project_user.project_id
            ).update({"multi_file_contexts": "{}", "multi_file_context_changes": "{}"})

    db.query(db_schemas.ProjectUser).filter(
        db_schemas.ProjectUser.user_id == user_id
    ).delete()
    db.query(db_schemas.Context).filter(
        db_schemas.Context.context_id.in_(
            list(map(lambda x: x.context_id, meta_queries))
        )
    ).delete()
    db.query(db_schemas.BehavioralTelemetry).filter(
        db_schemas.BehavioralTelemetry.behavioral_telemetry_id.in_(
            list(map(lambda x: x.behavioral_telemetry_id, meta_queries))
        )
    ).delete()
    db.query(db_schemas.ContextualTelemetry).filter(
        db_schemas.ContextualTelemetry.contextual_telemetry_id.in_(
            list(map(lambda x: x.contextual_telemetry_id, meta_queries))
        )
    ).delete()

    db.query(db_schemas.Session).filter(db_schemas.Session.user_id == user_id).delete()
    db.query(db_schemas.Chat).filter(db_schemas.Chat.user_id == user_id).delete()
    db.query(db_schemas.HadGeneration).filter(
        db_schemas.HadGeneration.meta_query_id.in_(
            list(map(lambda x: x.meta_query_id, meta_queries))
        )
    ).delete()
    db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).delete()
    db.commit()


# Context Operations
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
        multi_file_context_changes_indexes=json.dumps(
            query.multi_file_context_changes_indexes
        ),
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
    db: Session, query: Queries.CreateChatQuery, id: str = ""
) -> db_schemas.ChatQuery:
    meta_query_id = uuid.uuid4() if id == "" else uuid.UUID(id)

    # Step 1: Create MetaQuery first with all the main fields
    db_meta_query = db_schemas.MetaQuery(
        meta_query_id=meta_query_id,
        user_id=query.user_id,
        contextual_telemetry_id=query.contextual_telemetry_id,
        behavioral_telemetry_id=query.behavioral_telemetry_id,
        context_id=query.context_id,
        session_id=query.session_id,
        project_id=query.project_id,
        multi_file_context_changes_indexes=json.dumps(
            query.multi_file_context_changes_indexes
        ),
        timestamp=datetime.now(),
        total_serving_time=query.total_serving_time,
        server_version_id=query.server_version_id,
        query_type="chat",
    )

    # Step 2: Create ChatQuery with ONLY its specific fields
    db_chat_query = db_schemas.ChatQuery(
        meta_query_id=meta_query_id,
        chat_id=query.chat_id,
        web_enabled=query.web_enabled,
    )

    # Step 3: Save both to database
    db.add(db_meta_query)
    db.commit()
    db.add(db_chat_query)
    db.commit()
    db.refresh(db_meta_query)
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


def create_generation(
    db: Session, generation: Queries.CreateGeneration, id: str = ""
) -> db_schemas.HadGeneration:
    # Convert string timestamps to datetime objects
    shown_at_datetimes = [datetime.fromisoformat(ts) for ts in generation.shown_at]

    db_generation = db_schemas.HadGeneration(
        meta_query_id=uuid.uuid4() if id == "" else uuid.UUID(id),
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


# def update_generation_acceptance(
#     db: Session, update_data: Queries.UpdateGenerationAcceptance
# ) -> Optional[db_schemas.HadGeneration]:
#     """Update generation acceptance status"""
#     generation = get_generation_by_meta_query_and_model(
#         db, update_data.meta_query_id, update_data.model_id
#     )
#     if generation:
#         setattr(generation, "was_accepted", update_data.was_accepted)
#         db.commit()
#         db.refresh(generation)
#     return generation


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
        prompt_templates=model.prompt_templates,
        model_parameters=model.model_parameters,
    )
    db.add(db_model)
    db.commit()
    # db.refresh(db_model)
    return db_model


def update_generation(
    db: Session, query_id: str, model_id: int, generation: Queries.UpdateGeneration
) -> int:
    """Update an existing generation"""
    update_data = generation.dict(exclude_unset=True, to_json_values=True)
    result = (
        db.query(db_schemas.HadGeneration)
        .filter(
            db_schemas.HadGeneration.meta_query_id == query_id,
            db_schemas.HadGeneration.model_id == model_id,
        )
        .update(update_data)  # type: ignore
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
def create_chat(db: Session, chat: Queries.CreateChat, chat_id: str) -> db_schemas.Chat:
    # check if chat_id is already present in the database or not
    chat_uuid = uuid.UUID(chat_id) if isinstance(chat_id, str) else chat_id
    if existing_chat := get_chat_by_id(db, chat_uuid):
        if (
            existing_chat.user_id != chat.user_id
            or existing_chat.project_id != chat.project_id
        ):
            raise ValueError("Chat ID already exists with different project/user.")
        else:
            # If chat already exists, just return it
            return existing_chat

    db_chat = db_schemas.Chat(
        chat_id=chat_uuid if chat_id else uuid.uuid4(),
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
    update_data = chat_update.dict(exclude_unset=True, to_json_values=True)
    result = (
        db.query(db_schemas.Chat)
        .filter(db_schemas.Chat.chat_id == chat_id)
        .update(update_data)  # type: ignore
    )
    db.commit()
    return result


def get_chats_for_project(db: Session, project_id: uuid.UUID) -> list[db_schemas.Chat]:
    return (
        db.query(db_schemas.Chat).filter(db_schemas.Chat.project_id == project_id).all()
    )


def get_project_chat_history(
    db: Session, project_id: uuid.UUID, user_id: uuid.UUID, page_number: int = 1
) -> list[
    tuple[
        db_schemas.Chat,
        tuple[db_schemas.MetaQuery, db_schemas.Context, list[db_schemas.HadGeneration]],
    ]
]:
    """
    Get the chat history for a specific project and user.
    Returns a list of chats ordered by creation date.
    """
    chats = (
        db.query(db_schemas.Chat)
        .filter(
            db_schemas.Chat.project_id == project_id, db_schemas.Chat.user_id == user_id
        )
        .order_by(db_schemas.Chat.created_at.desc())
        .offset((page_number - 1) * 10)
        .limit(10)
        .all()
    )

    if not chats:
        return []

    history_page = []

    # per chat, get the entire chat history
    for chat in chats:
        information = get_chat_history(db, chat.chat_id)
        if information:
            history_page.append((chat, information))

    return history_page


def get_chats_for_user(db: Session, user_id: uuid.UUID) -> list[db_schemas.Chat]:
    return db.query(db_schemas.Chat).filter(db_schemas.Chat.user_id == user_id).all()


def get_chat_history(
    db: Session, chat_id: uuid.UUID
) -> list[
    tuple[db_schemas.MetaQuery, db_schemas.Context, list[db_schemas.HadGeneration]]
]:
    """
    Get the complete chat history for a specific chat ID.
    Returns a list of tuples containing (meta_query, context, generations)
    ordered by timestamp.
    """
    # Get all chat queries for this chat
    chat_queries = get_chat_queries_for_chat(db, chat_id)

    # Get the chat metadata
    chat = get_chat_by_id(db, chat_id)
    if not chat:
        return []

    # Build the history
    history = []
    for chat_query in chat_queries:
        # Get the meta query
        meta_query = get_meta_query_by_id(db, chat_query.meta_query_id)
        if not meta_query:
            continue

        # Get the context (contains user message)
        context = get_context_by_id(db, meta_query.context_id)
        if not context:
            continue

        # Get all generations for this query
        generations = get_generations_by_meta_query_id(
            db, str(meta_query.meta_query_id)
        )

        # Add to history
        history.append((meta_query, context, generations))

    # Sort by timestamp
    history.sort(key=lambda x: x[0].timestamp)

    return history


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
def create_project(
    db: Session, project: Queries.CreateProject, id: str = ""
) -> db_schemas.Project:
    db_project = db_schemas.Project(
        project_id=uuid.uuid4() if id == "" else uuid.UUID(id),
        project_name=project.project_name,
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
) -> int:
    update_data = project_update.dict(exclude_unset=True, to_json_values=True)
    result = (
        db.query(db_schemas.Project)
        .filter(db_schemas.Project.project_id == project_id)
        .update(update_data)  # type: ignore
    )
    db.commit()
    return result


def get_projects_for_user(db: Session, user_id: uuid.UUID) -> list[db_schemas.Project]:
    return (
        db.query(db_schemas.Project)
        .join(db_schemas.ProjectUser)
        .filter(db_schemas.ProjectUser.user_id == user_id)
        .all()
    )


def create_user_project(
    db: Session, project_user: Queries.CreateUserProject
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


def get_user_project(
    db: Session, user_id: uuid.UUID, project_id: uuid.UUID
) -> Optional[db_schemas.ProjectUser]:
    return (
        db.query(db_schemas.ProjectUser)
        .filter(
            db_schemas.ProjectUser.user_id == user_id,
            db_schemas.ProjectUser.project_id == project_id,
        )
        .first()
    )


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


def get_session_project(
    db: Session, session_id: uuid.UUID, project_id: uuid.UUID
) -> Optional[db_schemas.SessionProject]:
    return (
        db.query(db_schemas.SessionProject)
        .filter(
            db_schemas.SessionProject.session_id == session_id,
            db_schemas.SessionProject.project_id == project_id,
        )
        .first()
    )


def update_session(
    db: Session,
    session_id: uuid.UUID,
    session_update: Queries.UpdateSession,
) -> int:
    update_data = session_update.dict(exclude_unset=True, to_json_values=True)
    result = (
        db.query(db_schemas.Session)
        .filter(db_schemas.Session.session_id == session_id)
        .update(update_data)  # type: ignore
    )
    db.commit()
    return result


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


# Documentation Operations
def create_documentation(
    db: Session, doc: Queries.CreateDocumentation
) -> db_schemas.Documentation:
    """Create a new documentation entry with embedding."""

    # Generate embedding for the content
    try:
        embedding = encode_text(doc.content)
    except Exception as e:
        # If embedding fails, log error but don't fail the creation
        print(f"Warning: Failed to generate embedding: {e}")
        embedding = None

    db_doc = db_schemas.Documentation(
        content=doc.content, language=doc.language, embedding=embedding
    )

    db.add(db_doc)
    db.commit()
    db.refresh(db_doc)
    return db_doc


def get_documentation_by_id(
    db: Session, doc_id: int
) -> Optional[db_schemas.Documentation]:
    """Get documentation by ID."""
    return (
        db.query(db_schemas.Documentation)
        .filter(db_schemas.Documentation.documentation_id == doc_id)
        .first()
    )


def get_all_documentation(
    db: Session, language: Optional[str] = None, limit: Optional[int] = None
) -> List[db_schemas.Documentation]:
    """Get all documentation, optionally filtered by language."""
    query = db.query(db_schemas.Documentation)

    if language:
        query = query.filter(db_schemas.Documentation.language == language)

    query = query.order_by(db_schemas.Documentation.created_at.desc())

    if limit:
        query = query.limit(limit)

    return query.all()


def update_documentation(
    db: Session, doc_id: int, doc_update: Queries.UpdateDocumentation
) -> Optional[db_schemas.Documentation]:
    """Update documentation entry."""
    doc = get_documentation_by_id(db, doc_id)
    if not doc:
        return None

    update_data = doc_update.dict(exclude_unset=True)

    # If content is being updated, regenerate embedding
    if "content" in update_data:
        try:
            new_embedding = encode_text(update_data["content"])
            update_data["embedding"] = new_embedding
        except Exception as e:
            print(f"Warning: Failed to update embedding: {e}")

    for field, value in update_data.items():
        setattr(doc, field, value)

    db.commit()
    db.refresh(doc)
    return doc


def delete_documentation(db: Session, doc_id: int) -> bool:
    """Delete documentation entry."""
    result = (
        db.query(db_schemas.Documentation)
        .filter(db_schemas.Documentation.documentation_id == doc_id)
        .delete()
    )
    db.commit()
    return result > 0


def search_similar_documentation(
    db: Session, search_query: Queries.SearchDocumentation
) -> List[Tuple[db_schemas.Documentation, float]]:
    """
    Search for documentation similar to the given query text.

    Returns:
        List of tuples containing (documentation, similarity_score)
    """
    # Generate embedding for the search query
    try:
        query_embedding = encode_text(search_query.query_text)
    except Exception as e:
        print(f"Error generating embedding for search query: {e}")
        return []

    # Build the SQL query
    # Using cosine similarity with pgvector
    sql_parts = [
        "SELECT documentation_id, content, language, created_at,",
        "       1 - (embedding <=> :query_embedding) as similarity_score",
        "FROM documentation",
        "WHERE embedding IS NOT NULL",
    ]

    params = {"query_embedding": str(query_embedding)}

    # Add language filter if specified
    if search_query.language:
        sql_parts.append("AND language = :language")
        params["language"] = search_query.language

    # Add similarity threshold filter
    sql_parts.append("AND (1 - (embedding <=> :query_embedding)) >= :threshold")
    params["threshold"] = search_query.similarity_threshold

    # Order by similarity and limit results
    sql_parts.extend(["ORDER BY similarity_score DESC", "LIMIT :limit"])
    params["limit"] = search_query.limit

    sql_query = " ".join(sql_parts)

    try:
        result = db.execute(text(sql_query), params)
        rows = result.fetchall()

        # Convert results to documentation objects with similarity scores
        results = []
        for row in rows:
            # Create a documentation object
            doc = db_schemas.Documentation(
                documentation_id=row.documentation_id,
                content=row.content,
                language=row.language,
                created_at=row.created_at,
            )

            similarity_score = float(row.similarity_score)
            results.append((doc, similarity_score))

        return results

    except Exception as e:
        print(f"Error executing similarity search: {e}")
        return []


def regenerate_embeddings(db: Session, language: Optional[str] = None) -> int:
    """
    Regenerate embeddings for all documentation entries.
    Useful when changing embedding models or fixing corrupted embeddings.

    Args:
        language: Optional language filter to regenerate only specific language docs

    Returns:
        Number of embeddings regenerated
    """
    query = db.query(db_schemas.Documentation)

    if language:
        query = query.filter(db_schemas.Documentation.language == language)

    docs = query.all()

    updated_count = 0
    for doc in docs:
        try:
            new_embedding = encode_text(doc.content)
            doc.embedding = new_embedding
            updated_count += 1
        except Exception as e:
            print(f"Failed to regenerate embedding for doc {doc.documentation_id}: {e}")

    db.commit()
    return updated_count


def get_documentation_stats(db: Session) -> dict:
    """Get statistics about documentation entries."""
    total_docs = db.query(db_schemas.Documentation).count()

    docs_with_embeddings = (
        db.query(db_schemas.Documentation)
        .filter(db_schemas.Documentation.embedding.isnot(None))
        .count()
    )

    # Get language distribution
    language_stats = (
        db.query(
            db_schemas.Documentation.language,
            func.count(db_schemas.Documentation.documentation_id),
        )
        .group_by(db_schemas.Documentation.language)
        .all()
    )

    return {
        "total_documents": total_docs,
        "documents_with_embeddings": docs_with_embeddings,
        "embedding_coverage": (
            docs_with_embeddings / total_docs if total_docs > 0 else 0
        ),
        "languages": dict(language_stats),
    }
