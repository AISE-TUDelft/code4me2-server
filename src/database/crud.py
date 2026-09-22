import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional, Tuple, Type, Union

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import Queries as Queries
from database import db_schemas
from database.db_schemas import DEFAULT_USER_PREFERENCE
from database.embedding_service import encode_text
from utils import hash_password, verify_password
from research.canonical import canonical_hash
from database.research_schemas import AgentRelease
from research.study.agents.distributions import validate_profile_configuration
from research.study.agents.enums import QualificationStatus
from research.study.agents.registry import SELECTABLE_STATUSES
from research.study.agents.store import row_to_release


class ProfileReleaseError(ValueError):
    """Raised when a profile pins a missing or non-selectable release."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_profile_release(session: Session, release_id: Optional[str]) -> None:
    """Validate a supplied profile release without changing registry rows."""
    if release_id is None:
        return
    release = session.get(AgentRelease, release_id)
    if release is None:
        raise ProfileReleaseError(
            "RELEASE_UNRESOLVED", f"release {release_id!r} is not registered"
        )
    status = str(release.status or "").upper()
    if status in {QualificationStatus.RETIRED.value, QualificationStatus.BLOCKED.value}:
        raise ProfileReleaseError(
            "RELEASE_WITHDRAWN", f"release {release_id!r} is withdrawn"
        )
    if status not in {item.value for item in SELECTABLE_STATUSES}:
        raise ProfileReleaseError(
            "RELEASE_NOT_QUALIFIED", f"release {release_id!r} is not qualified"
        )


def _validate_profile_configuration(
    db: Session,
    *,
    name: str,
    framework_version: str,
    release_id: Optional[str],
    tools_json: str,
    approval_policy: str,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_steps: Optional[int] = None,
) -> None:
    """Enforce the shared profile↔release executable contract (ISSUE-03/17).

    The candidate fields are validated together against the rehydrated release
    and its stored conformance evidence, so a profile that cannot execute is
    rejected at create/update time with a typed reason. ``model`` /
    ``temperature`` / ``max_steps`` participate in the BYOA field-coverage
    check; omitting them would let an unmapped field through until study
    creation.
    """
    if release_id is None:
        raise ProfileReleaseError(
            "RELEASE_UNRESOLVED", "a profile must pin a registered release"
        )
    row = db.get(AgentRelease, release_id)
    if row is None:
        raise ProfileReleaseError(
            "RELEASE_UNRESOLVED", f"release {release_id!r} is not registered"
        )
    candidate = SimpleNamespace(
        name=name,
        framework_version=framework_version,
        release_id=release_id,
        tools_json=tools_json,
        approval_policy=approval_policy,
        model=model,
        temperature=temperature,
        max_steps=max_steps,
    )
    validate_profile_configuration(
        candidate, row_to_release(row), release_json=row.release_json
    )


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


def update_config(db: Session, config_id: int, config_data: str) -> Optional[db_schemas.Config]:
    """Update the JSON string of a config by ID."""
    cfg = get_config_by_id(db, config_id)
    if not cfg:
        return None
    cfg.config_data = config_data
    db.commit()
    db.refresh(cfg)
    return cfg


def delete_config(db: Session, config_id: int) -> bool:
    """Delete a config by ID. Returns True if deleted."""
    cfg = get_config_by_id(db, config_id)
    if not cfg:
        return False
    db.delete(cfg)
    db.commit()
    return True


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


# ── Agent CRUD ────────────────────────────────────────────────────────────────
#
# Content parameters (task_description, first_system_message, last_user_message,
# response_text, tool_arguments, tool_result, payload_json, diff_text,
# edit_delta_json) are optional and default to None. Callers are responsible for
# passing None unless the user's store_agent_content preference resolves True —
# see backend.routers.agent.consent.resolve_store_agent_content. These helpers
# deliberately do not re-check consent, so there is exactly one place where that
# decision is made.


class ProfileLockedError(PermissionError):
    """Raised when an active research study owns a profile selection."""


def _agent_profile_configuration(profile: db_schemas.AgentProfile) -> dict:
    return {
        "profile_id": str(profile.profile_id),
        "name": profile.name,
        "model": profile.model,
        "framework_version": profile.framework_version,
        "connection_id": str(profile.connection_id) if profile.connection_id else None,
        "release_id": profile.release_id,
        "tools_json": profile.tools_json,
        "approval_policy": profile.approval_policy,
        "max_steps": profile.max_steps,
        "temperature": profile.temperature,
        "max_context_tokens": profile.max_context_tokens,
        "is_active": profile.is_active,
    }


def _refresh_agent_profile_digest(profile: db_schemas.AgentProfile) -> None:
    profile.configuration_digest = canonical_hash(_agent_profile_configuration(profile))


def _assert_agent_profile_editable(db: Session, profile_id: uuid.UUID) -> None:
    from database.research_schemas import StudyAgentProfile

    linked_active = (
        db.query(StudyAgentProfile.study_id)
        .join(
            db_schemas.Study,
            StudyAgentProfile.study_id == db_schemas.Study.study_id,
        )
        .filter(
            StudyAgentProfile.profile_id == profile_id,
            db_schemas.Study.is_research.is_(True),
            db_schemas.Study.research_status == "ACTIVE",
        )
        .first()
    )
    if linked_active is not None:
        raise ProfileLockedError(
            "profile is locked while linked research study is ACTIVE"
        )


def create_agent_profile(
    db: Session,
    *,
    owner_user_id: uuid.UUID,
    name: str,
    model: str,
    tools_json: str,
    approval_policy: str,
    max_steps: int,
    framework_version: str = "code4me2-agent",
    connection_id: Optional[uuid.UUID] = None,
    release_id: Optional[str] = None,
    is_active: bool = True,
    max_context_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> db_schemas.AgentProfile:
    """Create a researcher-owned profile template.

    The provider endpoint/secret live on the referenced ``provider_connection``;
    a profile never stores a URL or a secret reference.
    """
    validate_profile_release(db, release_id)
    _validate_profile_configuration(
        db,
        name=name,
        framework_version=framework_version,
        release_id=release_id,
        tools_json=tools_json,
        approval_policy=approval_policy,
        model=model,
        temperature=temperature,
        max_steps=max_steps,
    )
    profile = db_schemas.AgentProfile(
        profile_id=uuid.uuid4(),
        owner_user_id=owner_user_id,
        name=name,
        model=model,
        framework_version=framework_version,
        connection_id=connection_id,
        release_id=release_id,
        tools_json=tools_json,
        approval_policy=approval_policy,
        max_steps=max_steps,
        is_active=is_active,
        max_context_tokens=max_context_tokens,
        temperature=temperature,
    )
    _refresh_agent_profile_digest(profile)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def get_agent_profile_by_id(
    db: Session, profile_id: uuid.UUID
) -> Optional[db_schemas.AgentProfile]:
    return (
        db.query(db_schemas.AgentProfile)
        .filter(db_schemas.AgentProfile.profile_id == profile_id)
        .first()
    )


def list_agent_profiles(
    db: Session, owner_user_id: Optional[uuid.UUID] = None
) -> List[db_schemas.AgentProfile]:
    """List profiles; ``owner_user_id`` scopes to one researcher (admin = all)."""
    query = db.query(db_schemas.AgentProfile)
    if owner_user_id is not None:
        query = query.filter(db_schemas.AgentProfile.owner_user_id == owner_user_id)
    return query.order_by(db_schemas.AgentProfile.name).all()


def update_agent_profile(
    db: Session,
    profile_id: uuid.UUID,
    *,
    name: Optional[str] = None,
    model: Optional[str] = None,
    tools_json: Optional[str] = None,
    approval_policy: Optional[str] = None,
    max_steps: Optional[int] = None,
    framework_version: Optional[str] = None,
    connection_id: Optional[uuid.UUID] = None,
    release_id: Optional[str] = None,
    is_active: Optional[bool] = None,
    max_context_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    update_connection_id: bool = False,
    update_release_id: bool = False,
) -> Optional[db_schemas.AgentProfile]:
    """Update a profile template in place (caller performs ownership checks)."""
    profile = (
        db.query(db_schemas.AgentProfile)
        .filter(db_schemas.AgentProfile.profile_id == profile_id)
        .with_for_update()
        .first()
    )
    if profile is None:
        return None
    _assert_agent_profile_editable(db, profile_id)
    if update_release_id:
        validate_profile_release(db, release_id)
    # Validate the merged result, not just the supplied fields: changing only
    # the framework (or only the release) must not leave an unexecutable pair.
    _validate_profile_configuration(
        db,
        name=name if name is not None else profile.name,
        framework_version=(
            framework_version if framework_version is not None else profile.framework_version
        ),
        release_id=release_id if update_release_id else profile.release_id,
        tools_json=tools_json if tools_json is not None else profile.tools_json,
        approval_policy=(
            approval_policy if approval_policy is not None else profile.approval_policy
        ),
        model=model if model is not None else profile.model,
        temperature=(
            temperature if temperature is not None else profile.temperature
        ),
        max_steps=max_steps if max_steps is not None else profile.max_steps,
    )
    if name is not None:
        profile.name = name
    if model is not None:
        profile.model = model
    if framework_version is not None:
        profile.framework_version = framework_version
    if tools_json is not None:
        profile.tools_json = tools_json
    if approval_policy is not None:
        profile.approval_policy = approval_policy
    if max_steps is not None:
        profile.max_steps = max_steps
    if update_connection_id:
        profile.connection_id = connection_id
    if update_release_id:
        profile.release_id = release_id
    if is_active is not None:
        profile.is_active = is_active
    if max_context_tokens is not None:
        profile.max_context_tokens = max_context_tokens
    if temperature is not None:
        profile.temperature = temperature
    _refresh_agent_profile_digest(profile)
    db.commit()
    db.refresh(profile)
    return profile


def delete_agent_profile(db: Session, profile_id: uuid.UUID) -> bool:
    """Archive a profile template (never erase a frozen study snapshot)."""
    profile = (
        db.query(db_schemas.AgentProfile)
        .filter(db_schemas.AgentProfile.profile_id == profile_id)
        .with_for_update()
        .first()
    )
    if profile is None:
        return False
    _assert_agent_profile_editable(db, profile_id)
    if not profile.is_active:
        return True
    profile.is_active = False
    db.commit()
    return True


# ── Admin-managed provider connections (role + readiness) ──────────────────


def create_provider_connection(
    db: Session,
    *,
    label: str,
    base_url: str,
    secret_ref: str,
    models_json: str,
    is_active: bool = True,
) -> db_schemas.ProviderConnection:
    connection = db_schemas.ProviderConnection(
        connection_id=uuid.uuid4(),
        label=label,
        base_url=base_url,
        secret_ref=secret_ref,
        models_json=models_json,
        is_active=is_active,
    )
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def get_provider_connection(
    db: Session, connection_id: uuid.UUID
) -> Optional[db_schemas.ProviderConnection]:
    return db.get(db_schemas.ProviderConnection, connection_id)


def get_provider_connection_by_label(
    db: Session, label: str
) -> Optional[db_schemas.ProviderConnection]:
    return (
        db.query(db_schemas.ProviderConnection)
        .filter(db_schemas.ProviderConnection.label == label)
        .first()
    )


def list_provider_connections(db: Session) -> List[db_schemas.ProviderConnection]:
    return (
        db.query(db_schemas.ProviderConnection)
        .order_by(db_schemas.ProviderConnection.label)
        .all()
    )


def update_provider_connection(
    db: Session,
    connection_id: uuid.UUID,
    *,
    label: Optional[str] = None,
    base_url: Optional[str] = None,
    secret_ref: Optional[str] = None,
    models_json: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[db_schemas.ProviderConnection]:
    connection = db.get(db_schemas.ProviderConnection, connection_id)
    if connection is None:
        return None
    if label is not None:
        connection.label = label
    if base_url is not None:
        connection.base_url = base_url
    if secret_ref is not None:
        connection.secret_ref = secret_ref
    if models_json is not None:
        connection.models_json = models_json
    if is_active is not None:
        connection.is_active = is_active
    db.commit()
    db.refresh(connection)
    return connection


def delete_provider_connection(db: Session, connection_id: uuid.UUID) -> bool:
    connection = db.get(db_schemas.ProviderConnection, connection_id)
    if connection is None:
        return False
    db.delete(connection)
    db.commit()
    return True


def list_available_provider_connections(
    db: Session, user_id: uuid.UUID
) -> List[db_schemas.ProviderConnection]:
    """Active admin-managed connections available to an authorized researcher."""
    return (
        db.query(db_schemas.ProviderConnection)
        .filter(db_schemas.ProviderConnection.is_active.is_(True))
        .order_by(db_schemas.ProviderConnection.label)
        .all()
    )


def provider_connection_is_available(
    db: Session, connection_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Whether an active admin-managed connection can be selected."""
    return (
        db.query(db_schemas.ProviderConnection)
        .filter(
            db_schemas.ProviderConnection.connection_id == connection_id,
            db_schemas.ProviderConnection.is_active.is_(True),
        )
        .first()
        is not None
    )


# ── Researcher enablement ────────────────────────────────────────────────────


def set_user_can_research(
    db: Session, user_id: uuid.UUID, enabled: bool
) -> Optional[db_schemas.User]:
    """Administrator toggle of the single researcher-enablement flag."""
    user = db.get(db_schemas.User, user_id)
    if user is None:
        return None
    user.can_research = enabled
    db.commit()
    db.refresh(user)
    return user


def list_accounts(db: Session, limit: int = 100) -> List[db_schemas.User]:
    """Every account, newest first, for the administrator account view.

    The admin panel toggles ``can_research`` per account, so it needs the full
    account list rather than only the already-enabled researchers.
    """
    return (
        db.query(db_schemas.User)
        .order_by(db_schemas.User.joined_at.desc())
        .limit(limit)
        .all()
    )


# ── Agent tasks ─────────────────────────────────────────────────────────────


def create_agent_task(
    db: Session,
    agent_profile: str,
    model: str,
    approval_policy: str,
    tools_json: str,
    task_description: Optional[str] = None,
    session_id: Optional[uuid.UUID] = None,
    task_id: Optional[uuid.UUID] = None,
    temperature: Optional[float] = None,
    framework_version: Optional[str] = None,
    source: str = "plugin",
    owner_user_id: Optional[uuid.UUID] = None,
    funding_owner_user_id: Optional[uuid.UUID] = None,
    owner_project_id: Optional[uuid.UUID] = None,
    external_run_id: Optional[str] = None,
    agent_session_id: Optional[str] = None,
    status: str = "pending",
    started_at: Optional[datetime] = None,
    policy_snapshot: Optional[dict] = None,
    study_id: Optional[uuid.UUID] = None,
    study_assignment_id: Optional[uuid.UUID] = None,
    profile_id: Optional[uuid.UUID] = None,
    consent_content_storage: Optional[bool] = None,
    research_session_id: Optional[uuid.UUID] = None,
    enrollment_id: Optional[uuid.UUID] = None,
) -> db_schemas.AgentTask:
    task = db_schemas.AgentTask(
        task_id=task_id or uuid.uuid4(),
        agent_profile=agent_profile,
        model=model,
        temperature=temperature,
        approval_policy=approval_policy,
        tools_json=tools_json,
        framework_version=framework_version,
        status=status,
        source=source,
        session_id=session_id,
        owner_user_id=owner_user_id,
        funding_owner_user_id=funding_owner_user_id,
        owner_project_id=owner_project_id,
        external_run_id=external_run_id,
        agent_session_id=agent_session_id,
        study_id=study_id,
        study_assignment_id=study_assignment_id,
        profile_id=profile_id,
        consent_content_storage=consent_content_storage,
        research_session_id=research_session_id,
        enrollment_id=enrollment_id,
        started_at=started_at,
        policy_snapshot=policy_snapshot,
        task_description=task_description,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_agent_task(db: Session, task_id: uuid.UUID) -> Optional[db_schemas.AgentTask]:
    return (
        db.query(db_schemas.AgentTask)
        .filter(db_schemas.AgentTask.task_id == task_id)
        .first()
    )


def get_agent_task_by_external_run_id(
    db: Session, external_run_id: str
) -> Optional[db_schemas.AgentTask]:
    """Look a task up by the *runtime's* own run id.

    The self-reporting ``code4me2-agent`` runtime mints its own run id before it
    ever talks to the backend, so ingestion resolves the task this way rather
    than requiring the runtime to adopt a server-issued UUID.
    """
    return (
        db.query(db_schemas.AgentTask)
        .filter(db_schemas.AgentTask.external_run_id == external_run_id)
        .first()
    )


def get_open_agent_tasks_for_session(
    db: Session, session_id: uuid.UUID
) -> List[db_schemas.AgentTask]:
    """Tasks belonging to this session that haven't reached a terminal state yet."""
    return (
        db.query(db_schemas.AgentTask)
        .filter(
            db_schemas.AgentTask.session_id == session_id,
            db_schemas.AgentTask.status.notin_(["done", "failed"]),
        )
        .all()
    )


def update_agent_task_status(
    db: Session,
    task_id: uuid.UUID,
    status: str,
    completed_at: Optional[datetime] = None,
    total_steps: Optional[int] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    latest_event_id: Optional[uuid.UUID] = None,
) -> bool:
    data: dict = {"status": status}
    if completed_at is not None:
        data["completed_at"] = completed_at
    if total_steps is not None:
        data["total_steps"] = total_steps
    if input_tokens is not None:
        data["input_tokens"] = input_tokens
    if output_tokens is not None:
        data["output_tokens"] = output_tokens
    if latest_event_id is not None:
        data["latest_event_id"] = latest_event_id
    result = (
        db.query(db_schemas.AgentTask)
        .filter(db_schemas.AgentTask.task_id == task_id)
        .update(data)
    )
    db.commit()
    return result > 0


def update_agent_task_tools(
    db: Session,
    task_id: uuid.UUID,
    tools: List[str],
) -> None:
    db.query(db_schemas.AgentTask).filter(
        db_schemas.AgentTask.task_id == task_id
    ).update({"observed_tools_json": json.dumps(tools)})
    db.commit()


def set_agent_task_description(
    db: Session, task_id: uuid.UUID, description: str
) -> None:
    db.query(db_schemas.AgentTask).filter(
        db_schemas.AgentTask.task_id == task_id
    ).update({"task_description": description})
    db.commit()


def set_agent_task_framework_version(
    db: Session, task_id: uuid.UUID, framework_version: str
) -> None:
    db.query(db_schemas.AgentTask).filter(
        db_schemas.AgentTask.task_id == task_id
    ).update({"observed_framework_version": framework_version})
    db.commit()


# ── Agent events ────────────────────────────────────────────────────────────


def get_agent_events_by_task(
    db: Session,
    task_id: uuid.UUID,
    event_type: Optional[str] = None,
) -> List[db_schemas.AgentEvent]:
    q = db.query(db_schemas.AgentEvent).filter(db_schemas.AgentEvent.task_id == task_id)
    if event_type is not None:
        q = q.filter(db_schemas.AgentEvent.event_type == event_type)
    return q.order_by(db_schemas.AgentEvent.event_index).all()


def get_last_agent_event_for_session(
    db: Session,
    session_id: uuid.UUID,
    event_type: Optional[str] = None,
) -> Optional[db_schemas.AgentEvent]:
    """Most recent event across all tasks belonging to this session — used to detect
    chat-session boundaries that span idle-close task gaps."""
    q = (
        db.query(db_schemas.AgentEvent)
        .join(
            db_schemas.AgentTask,
            db_schemas.AgentEvent.task_id == db_schemas.AgentTask.task_id,
        )
        .filter(db_schemas.AgentTask.session_id == session_id)
    )
    if event_type is not None:
        q = q.filter(db_schemas.AgentEvent.event_type == event_type)
    return q.order_by(db_schemas.AgentEvent.created_at.desc()).first()


def reserve_agent_event_indexes(
    db: Session, task_id: uuid.UUID, event_count: int
) -> int:
    """Atomically reserve ``event_count`` task-local event indexes."""
    if event_count < 1:
        raise ValueError("event_count must be positive")
    first_index = db.execute(
        text(
            """
            UPDATE public.agent_task
            SET next_event_index = next_event_index + :event_count
            WHERE task_id = :task_id
            RETURNING next_event_index - :event_count
            """
        ),
        {"event_count": event_count, "task_id": task_id},
    ).scalar_one_or_none()
    if first_index is None:
        raise ValueError(f"Agent task {task_id} does not exist")
    return int(first_index)


def find_existing_agent_event_source_ids(
    db: Session,
    *,
    task_id: uuid.UUID,
    source: str,
    source_event_ids: List[str],
) -> List[str]:
    """Return source event ids already persisted for this task and source.

    This lookup is advisory only. The matching unique constraint is the
    authoritative protection against concurrent retries.
    """
    if not source_event_ids:
        return []
    return [
        row.source_event_id
        for row in db.query(db_schemas.AgentEvent.source_event_id)
        .filter(
            db_schemas.AgentEvent.task_id == task_id,
            db_schemas.AgentEvent.source == source,
            db_schemas.AgentEvent.source_event_id.in_(source_event_ids),
        )
        .all()
    ]


def append_agent_event(
    db: Session,
    task_id: uuid.UUID,
    event_index: int,
    event_type: str,
    latency_ms: Optional[int] = None,
    source: Optional[str] = None,
    source_event_id: Optional[str] = None,
    schema_version: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    # Shared span identifiers (model_call and tool_call)
    span_id: Optional[str] = None,
    parent_span_id: Optional[uuid.UUID] = None,
    request_id: Optional[str] = None,
    chat_session_index: Optional[int] = None,
    # model_call fields
    model: Optional[str] = None,
    agent_profile: Optional[str] = None,
    streaming: Optional[bool] = None,
    message_count: Optional[int] = None,
    role_system_count: Optional[int] = None,
    role_user_count: Optional[int] = None,
    role_assistant_count: Optional[int] = None,
    role_tool_count: Optional[int] = None,
    tools_kept: Optional[int] = None,
    tools_stripped: Optional[int] = None,
    tool_names_requested: Optional[list] = None,
    max_tokens: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None,
    finish_reason: Optional[str] = None,
    upstream_status: Optional[int] = None,
    step_index: Optional[int] = None,
    context_window_size_bytes: Optional[int] = None,
    active_file: Optional[str] = None,
    first_message_hash: Optional[str] = None,
    chat_new_session_detected: Optional[bool] = None,
    experiment_tool_access_enabled: Optional[bool] = None,
    experiment_approval_policy: Optional[str] = None,
    # model_call content (only when store_agent_content resolves True)
    first_system_message: Optional[str] = None,
    last_user_message: Optional[str] = None,
    response_text: Optional[str] = None,
    # tool_call fields
    tool_name: Optional[str] = None,
    tool_arguments_length: Optional[int] = None,
    tool_result_length: Optional[int] = None,
    # tool_call content (only when store_agent_content resolves True)
    tool_arguments: Optional[str] = None,
    tool_result: Optional[str] = None,
    # JSON overflow
    extra_json: Optional[str] = None,
    # content (only when store_agent_content resolves True)
    payload_json: Optional[str] = None,
    ignore_duplicate_source: bool = False,
    commit: bool = True,
) -> Optional[db_schemas.AgentEvent]:
    """Append one event row.

    ``commit=False`` lets a caller stage a whole batch and commit once (used by
    the self-report ingestion path, where a partially-written batch would leave
    gaps in ``event_index``).
    """
    event = db_schemas.AgentEvent(
        event_id=uuid.uuid4(),
        task_id=task_id,
        event_index=event_index,
        event_type=event_type,
        source=source,
        source_event_id=source_event_id,
        schema_version=schema_version,
        latency_ms=latency_ms,
        occurred_at=occurred_at,
        span_id=span_id,
        parent_span_id=parent_span_id,
        request_id=request_id,
        chat_session_index=chat_session_index,
        model=model,
        agent_profile=agent_profile,
        streaming=streaming,
        message_count=message_count,
        role_system_count=role_system_count,
        role_user_count=role_user_count,
        role_assistant_count=role_assistant_count,
        role_tool_count=role_tool_count,
        tools_kept=tools_kept,
        tools_stripped=tools_stripped,
        tool_names_requested=tool_names_requested,
        max_tokens=max_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        finish_reason=finish_reason,
        upstream_status=upstream_status,
        step_index=step_index,
        context_window_size_bytes=context_window_size_bytes,
        active_file=active_file,
        first_message_hash=first_message_hash,
        chat_new_session_detected=chat_new_session_detected,
        experiment_tool_access_enabled=experiment_tool_access_enabled,
        experiment_approval_policy=experiment_approval_policy,
        first_system_message=first_system_message,
        last_user_message=last_user_message,
        response_text=response_text,
        tool_name=tool_name,
        tool_arguments_length=tool_arguments_length,
        tool_result_length=tool_result_length,
        tool_arguments=tool_arguments,
        tool_result=tool_result,
        extra_json=extra_json,
        payload_json=payload_json,
    )
    if ignore_duplicate_source and source is not None and source_event_id is not None:
        values = {
            column.name: getattr(event, column.name)
            for column in db_schemas.AgentEvent.__table__.columns
        }
        inserted_event_id = db.execute(
            pg_insert(db_schemas.AgentEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_agent_event_source_identity")
            .returning(db_schemas.AgentEvent.event_id)
        ).scalar_one_or_none()
        if inserted_event_id is None:
            return None
        if commit:
            db.commit()
        return db.get(db_schemas.AgentEvent, inserted_event_id)
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


# ── Agent edits (human-in-the-loop decisions) ───────────────────────────────


def get_agent_memory_by_session_id(
    db: Session, session_id: str
) -> Optional[db_schemas.AgentMemory]:
    return (
        db.query(db_schemas.AgentMemory)
        .filter(db_schemas.AgentMemory.session_id == session_id)
        .first()
    )


def upsert_agent_memory(
    db: Session,
    *,
    session_id: str,
    owner_user_id: str,
    owner_project_id: str,
    messages: List[dict],
) -> db_schemas.AgentMemory:
    """Replace the stored memory snapshot for one agent session.

    Ownership is written from the server-derived ACP scope, never from the
    request body, so a client cannot attach its memory to someone else's
    session.
    """
    memory_json = json.dumps({"messages": messages}, sort_keys=True)
    existing_memory = get_agent_memory_by_session_id(db, session_id)
    now = datetime.now()
    if existing_memory is None:
        agent_memory = db_schemas.AgentMemory(
            session_id=session_id,
            owner_user_id=owner_user_id,
            owner_project_id=owner_project_id,
            memory_json=memory_json,
            created_at=now,
            updated_at=now,
        )
        db.add(agent_memory)
    else:
        existing_memory.owner_user_id = owner_user_id
        existing_memory.owner_project_id = owner_project_id
        existing_memory.memory_json = memory_json
        existing_memory.updated_at = now
        agent_memory = existing_memory
    db.commit()
    db.refresh(agent_memory)
    return agent_memory


def delete_agent_memory(db: Session, session_id: str) -> bool:
    result = (
        db.query(db_schemas.AgentMemory)
        .filter(db_schemas.AgentMemory.session_id == session_id)
        .delete()
    )
    db.commit()
    return result > 0
