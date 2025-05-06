import uuid
from datetime import datetime
from typing import Type, Optional, Union

from sqlalchemy.orm import Session

import Queries as Queries
from database import db_schemas
from database.utils import hash_password


# READ operation
# User Table


def get_user_by_email(db: Session, email: str) -> Optional[Type[db_schemas.User]]:
    return db.query(db_schemas.User).filter(db_schemas.User.email == email).first()


def get_user_by_email_password(
    db: Session, email: str, password: str
) -> Optional[Type[db_schemas.User]]:
    user = db.query(db_schemas.User).filter(db_schemas.User.email == email).first()
    if user and user.password_hash == hash_password(password):
        return user
    return None


# CREATE operations


# Create new user (authentication version)
def create_user(
    db: Session, user: Union[Queries.CreateUser, Queries.CreateUserAuth]
) -> db_schemas.User:
    # Create user object
    db_user = db_schemas.User(
        user_id=str(uuid.uuid4()),
        joined_at=datetime.now().isoformat(),
        email=str(user.email),
        name=user.name,
        password_hash=hash_password(user.password.get_secret_value()),
        is_oauth_signup=isinstance(user, Queries.CreateUserAuth),
        verified=False,
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

    #
    #
    # # Set user verification status
    # def set_user_verified(
    #     db: Session, user_token: str, verified: bool = True
    # ) -> db_schemas.User:
    #     user = self.get_user_by_token(user_token)
    #     if user:
    #         user.verified = verified
    #         db.commit()
    #         db.refresh(user)
    #     return user
    #
    #
    # def get_all_users(self) -> list[Type[db_schemas.User]]:
    #     return db.query(db_schemas.User).all()
    #
    #


def get_user_by_id(db: Session, user_id: str) -> Optional[db_schemas.User]:
    return db.query(db_schemas.User).filter(db_schemas.User.user_id == user_id).first()

    #
    #
    # # Query Table
    # def get_all_queries(self) -> list[db_schemas.Query]:
    #     return db.query(db_schemas.Query).all()
    #
    #
    # def get_query_by_id(db: Session, query_id: str) -> db_schemas.Query:
    #     assert is_valid_uuid(query_id)
    #     return (
    #         db.query(db_schemas.Query).filter(db_schemas.Query.query_id == query_id).first()
    #     )
    #
    #
    # def get_user_queries(db: Session, user_id: str) -> list[db_schemas.Query]:
    #     assert is_valid_uuid(user_id)
    #     return db.query(db_schemas.Query).filter(db_schemas.Query.user_id == user_id).all()
    #
    #
    # # TODO: change this to db_schemas.Query
    # def get_queries_in_time_range(
    #     db: Session, start_time: str = None, end_time: str = None
    # ) -> list[Type[db_schemas.Query]]:
    #     if start_time and end_time:
    #         return (
    #             db.query(db_schemas.Query)
    #             .filter(
    #                 db_schemas.Query.timestamp >= start_time,
    #                 db_schemas.Query.timestamp <= end_time,
    #             )
    #             .all()
    #         )
    #     elif start_time:
    #         return (
    #             db.query(db_schemas.Query)
    #             .filter(db_schemas.Query.timestamp >= start_time)
    #             .all()
    #         )
    #     elif end_time:
    #         return (
    #             db.query(db_schemas.Query)
    #             .filter(db_schemas.Query.timestamp <= end_time)
    #             .all()
    #         )
    #     return db.query(db_schemas.Query).all()
    #
    #
    # # TODO: change this to db_schemas.Query
    # def get_queries_bound_by_context(db: Session, context_id: str) -> list[Type[db_schemas.Query]]:
    #     assert is_valid_uuid(context_id)
    #     return (
    #         db.query(db_schemas.Query).filter(db_schemas.Query.context_id == context_id).all()
    #     )
    #
    #
    # def get_query_by_telemetry_id(db: Session, telemetry_id: str) -> db_schemas.Query:
    #     assert is_valid_uuid(telemetry_id)
    #     return (
    #         db.query(db_schemas.Query)
    #         .filter(db_schemas.Query.telemetry_id == telemetry_id)
    #         .first()
    #     )
    #
    #
    # # programming_language Table
    # def get_all_programming_languages(
    #     self
    # ) -> list[Type[db_schemas.ProgrammingLanguage]]:
    #     return db.query(db_schemas.ProgrammingLanguage).all()
    #
    #
    # def get_programming_language_by_id(
    #     db: Session, language_id: int
    # ) -> db_schemas.ProgrammingLanguage:
    #     return (
    #         db.query(db_schemas.ProgrammingLanguage)
    #         .filter(db_schemas.ProgrammingLanguage.language_id == language_id)
    #         .first()
    #     )
    #
    #
    # def get_programming_language_by_name(
    #     db: Session, language_name: str
    # ) -> db_schemas.ProgrammingLanguage:
    #     return (
    #         db.query(db_schemas.ProgrammingLanguage)
    #         .filter(db_schemas.ProgrammingLanguage.language_name == language_name)
    #         .first()
    #     )
    #
    #
    # # had_generation Table
    # def get_all_generations(self) -> list[Type[db_schemas.HadGeneration]]:
    #     return db.query(db_schemas.HadGeneration).all()
    #
    #
    # def get_generations_by_query_id(
    #     db: Session, query_id: str
    # ) -> list[Type[db_schemas.HadGeneration]]:
    #     assert is_valid_uuid(query_id)
    #     return (
    #         db.query(db_schemas.HadGeneration)
    #         .filter(db_schemas.HadGeneration.query_id == query_id)
    #         .all()
    #     )
    #
    #
    # def get_generations_by_query_and_model_id(
    #     db: Session, query_id: str, model_id: int
    # ) -> Type[db_schemas.HadGeneration]:
    #     assert is_valid_uuid(query_id)
    #     return (
    #         db.query(db_schemas.HadGeneration)
    #         .filter(
    #             db_schemas.HadGeneration.query_id == query_id,
    #             db_schemas.HadGeneration.model_id == model_id,
    #         )
    #         .first()
    #     )
    #
    #
    # def get_generations_having_confidence_in_range(
    #     db: Session, lower_bound: float = None, upper_bound: float = None
    # ) -> list[Type[db_schemas.HadGeneration]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(
    #                 db_schemas.HadGeneration.confidence >= lower_bound,
    #                 db_schemas.HadGeneration.confidence <= upper_bound,
    #             )
    #             .all()
    #         )
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(db_schemas.HadGeneration.confidence >= lower_bound)
    #             .all()
    #         )
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(db_schemas.HadGeneration.confidence <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_generations()
    #
    #
    # def get_generations_having_acceptance_of(
    #     db: Session, acceptance: bool
    # ) -> list[Type[db_schemas.HadGeneration]]:
    #     return (
    #         db.query(db_schemas.HadGeneration)
    #         .filter(db_schemas.HadGeneration.was_accepted == acceptance)
    #         .all()
    #     )
    #
    #
    # def get_generations_with_shown_times_in_range(
    #     db: Session, lower_bound: int = None, upper_bound: int = None
    # ) -> list[Type[db_schemas.HadGeneration]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(
    #                 func.cardinality(db_schemas.HadGeneration.shown_at) >= lower_bound,
    #                 func.cardinality(db_schemas.HadGeneration.shown_at) <= upper_bound,
    #             )
    #             .all()
    #         )
    #
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(func.cardinality(db_schemas.HadGeneration.shown_at) >= lower_bound)
    #             .all()
    #         )
    #
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.HadGeneration)
    #             .filter(func.cardinality(db_schemas.HadGeneration.shown_at) <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_generations()
    #
    #
    # # model_name Table
    # def get_all_db_schemas(self) -> list[Type[db_schemas.ModelName]]:
    #     return db.query(db_schemas.ModelName).all()
    #
    #
    # def get_model_by_id(db: Session, model_id: int) -> db_schemas.ModelName:
    #     return (
    #         db.query(db_schemas.ModelName)
    #         .filter(db_schemas.ModelName.model_id == model_id)
    #         .first()
    #     )
    #
    #
    # def get_model_by_name(db: Session, model_name: str) -> db_schemas.ModelName:
    #     return (
    #         db.query(db_schemas.ModelName)
    #         .filter(db_schemas.ModelName.model_name == model_name)
    #         .first()
    #     )
    #
    #
    # # ground_truth Table
    # def get_all_ground_truths(self) -> list[Type[db_schemas.GroundTruth]]:
    #     return db.query(db_schemas.GroundTruth).all()
    #
    #
    # def get_ground_truths_for(
    #     db: Session, query_id: str
    # ) -> list[Type[db_schemas.GroundTruth]]:
    #     assert is_valid_uuid(query_id)
    #     return (
    #         db.query(db_schemas.GroundTruth)
    #         .filter(db_schemas.GroundTruth.query_id == query_id)
    #         .all()
    #     )
    #
    #
    # def get_ground_truths_for_query_in_time_range(
    #     db: Session, query_id: str, start_time: str = None, end_time: str = None
    # ) -> list[Type[db_schemas.GroundTruth]]:
    #     assert is_valid_uuid(query_id)
    #     if start_time and end_time:
    #         return (
    #             db.query(db_schemas.GroundTruth)
    #             .filter(
    #                 db_schemas.GroundTruth.query_id == query_id,
    #                 db_schemas.GroundTruth.truth_timestamp >= start_time,
    #                 db_schemas.GroundTruth.truth_timestamp <= end_time,
    #             )
    #             .all()
    #         )
    #     elif start_time:
    #         return (
    #             db.query(db_schemas.GroundTruth)
    #             .filter(
    #                 db_schemas.GroundTruth.query_id == query_id,
    #                 db_schemas.GroundTruth.truth_timestamp >= start_time,
    #             )
    #             .all()
    #         )
    #     elif end_time:
    #         return (
    #             db.query(db_schemas.GroundTruth)
    #             .filter(
    #                 db_schemas.GroundTruth.query_id == query_id,
    #                 db_schemas.GroundTruth.truth_timestamp <= end_time,
    #             )
    #             .all()
    #         )
    #
    #     return self.get_ground_truths_for(query_id)
    #
    #
    # # telemetry table
    # def get_all_telemetries(self) -> list[Type[db_schemas.Telemetry]]:
    #     return db.query(db_schemas.Telemetry).all()
    #
    #
    # def get_telemetries_with_time_since_last_completion_in_range(
    #     db: Session, lower_bound: int = None, upper_bound: int = None
    # ) -> list[Type[db_schemas.Telemetry]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(
    #                 db_schemas.Telemetry.time_since_last_completion >= lower_bound,
    #                 db_schemas.Telemetry.time_since_last_completion <= upper_bound,
    #             )
    #             .all()
    #         )
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.time_since_last_completion >= lower_bound)
    #             .all()
    #         )
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.time_since_last_completion <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_telemetries()
    #
    #
    # def get_telemetry_by_id(db: Session, telemetry_id: str) -> db_schemas.Telemetry:
    #     assert is_valid_uuid(telemetry_id)
    #     return (
    #         db.query(db_schemas.Telemetry)
    #         .filter(db_schemas.Telemetry.telemetry_id == telemetry_id)
    #         .first()
    #     )
    #
    #
    # def get_telemetries_with_typing_speed_in_range(
    #     db: Session, lower_bound: int = None, upper_bound: int = None
    # ) -> list[Type[db_schemas.Telemetry]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(
    #                 db_schemas.Telemetry.typing_speed >= lower_bound,
    #                 db_schemas.Telemetry.typing_speed <= upper_bound,
    #             )
    #             .all()
    #         )
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.typing_speed >= lower_bound)
    #             .all()
    #         )
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.typing_speed <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_telemetries()
    #
    #
    # def get_telemetries_with_document_char_length_in_range(
    #     db: Session, lower_bound: int = None, upper_bound: int = None
    # ) -> list[Type[db_schemas.Telemetry]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(
    #                 db_schemas.Telemetry.document_char_length >= lower_bound,
    #                 db_schemas.Telemetry.document_char_length <= upper_bound,
    #             )
    #             .all()
    #         )
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.document_char_length >= lower_bound)
    #             .all()
    #         )
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.document_char_length <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_telemetries()
    #
    #
    # def get_telemetries_with_relative_document_position_in_range(
    #     db: Session, lower_bound: float = None, upper_bound: float = None
    # ) -> list[Type[db_schemas.Telemetry]]:
    #     if lower_bound and upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(
    #                 db_schemas.Telemetry.relative_document_position >= lower_bound,
    #                 db_schemas.Telemetry.relative_document_position <= upper_bound,
    #             )
    #             .all()
    #         )
    #     elif lower_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.relative_document_position >= lower_bound)
    #             .all()
    #         )
    #     elif upper_bound:
    #         return (
    #             db.query(db_schemas.Telemetry)
    #             .filter(db_schemas.Telemetry.relative_document_position <= upper_bound)
    #             .all()
    #         )
    #
    #     return self.get_all_telemetries()
    #
    #
    # # context Table
    # def get_all_contexts(self) -> list[Type[db_schemas.Context]]:
    #     return db.query(db_schemas.Context).all()
    #
    #
    # def get_context_by_id(db: Session, context_id: str) -> db_schemas.Context:
    #     assert is_valid_uuid(context_id)
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.context_id == context_id)
    #         .first()
    #     )
    #
    #
    # def get_contexts_where_language_is(
    #     db: Session, language_id: int
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.language_id == language_id)
    #         .all()
    #     )
    #
    #
    # def get_contexts_where_trigger_type_is(
    #     db: Session, trigger_type_id: int
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.trigger_type_id == trigger_type_id)
    #         .all()
    #     )
    #
    #
    # def get_contexts_where_version_is(
    #     db: Session, version_id: int
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.version_id == version_id)
    #         .all()
    #     )
    #
    #
    # def get_contexts_where_prefix_contains(
    #     db: Session, text: str
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.prefix.contains(text))
    #         .all()
    #     )
    #
    #
    # def get_contexts_where_suffix_contains(
    #     db: Session, text: str
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(db_schemas.Context.suffix.contains(text))
    #         .all()
    #     )
    #
    #
    # def get_contexts_where_prefix_or_suffix_contains(
    #     db: Session, text: str
    # ) -> list[Type[db_schemas.Context]]:
    #     return (
    #         db.query(db_schemas.Context)
    #         .filter(
    #             db_schemas.Context.prefix.contains(text),
    #             db_schemas.Context.suffix.contains(text),
    #         )
    #         .all()
    #     )
    #
    #
    # # trigger_type Table
    # def get_all_trigger_types(self) -> list[Type[db_schemas.TriggerType]]:
    #     return db.query(db_schemas.TriggerType).all()
    #
    #
    # def get_trigger_type_by_id(db: Session, trigger_type_id: int) -> db_schemas.TriggerType:
    #     return (
    #         db.query(db_schemas.TriggerType)
    #         .filter(db_schemas.TriggerType.trigger_type_id == trigger_type_id)
    #         .first()
    #     )
    #
    #
    # def get_trigger_type_by_name(
    #     db: Session, trigger_type_name: str
    # ) -> db_schemas.TriggerType:
    #     return (
    #         db.query(db_schemas.TriggerType)
    #         .filter(db_schemas.TriggerType.trigger_type_name == trigger_type_name)
    #         .first()
    #     )
    #
    #
    # # plugin_version Table
    # def get_all_plugin_versions(self) -> list[Type[db_schemas.PluginVersion]]:
    #     return db.query(db_schemas.PluginVersion).all()
    #
    #
    # def get_plugin_version_by_id(db: Session, version_id: int) -> db_schemas.PluginVersion:
    #     return (
    #         db.query(db_schemas.PluginVersion)
    #         .filter(db_schemas.PluginVersion.version_id == version_id)
    #         .first()
    #     )
    #
    #
    # def get_plugin_versions_by_ide_type(
    #     db: Session, ide_type: str
    # ) -> list[Type[db_schemas.PluginVersion]]:
    #     return (
    #         db.query(db_schemas.PluginVersion)
    #         .filter(db_schemas.PluginVersion.ide_type == ide_type)
    #         .all()
    #     )
    #
    #
    # def get_plugin_versions_by_name_containing(
    #     db: Session, version_name: str
    # ) -> list[Type[db_schemas.PluginVersion]]:
    #     return (
    #         db.query(db_schemas.PluginVersion)
    #         .filter(db_schemas.PluginVersion.version_name.contains(version_name))
    #         .all()
    #     )
    #
    #
    # def get_plugin_versions_by_description_containing(
    #     db: Session, description: str
    # ) -> list[Type[db_schemas.PluginVersion]]:
    #     return (
    #         db.query(db_schemas.PluginVersion)
    #         .filter(db_schemas.PluginVersion.description.contains(description))
    #         .all()
    #     )
    #
    #
    # # CREATE operations
    # # Simple CREATE operations with complete data -> nothing has to be checked or generated/calculated before creating
    # def create_user(db: Session, user: db_schemas.UserCreate) -> db_schemas.User:
    #     db_user = db_schemas.User(**user.model_dump())
    #     db.add(db_user)
    #     db.commit()
    #     db.refresh(db_user)
    #     return db_user
    #
    #
    # def add_programming_language(
    #     db: Session, language: db_schemas.ProgrammingLanguageCreate
    # ) -> db_schemas.ProgrammingLanguage:
    #     db_language = db_schemas.ProgrammingLanguage(**language.model_dump())
    #     db.add(db_language)
    #     db.commit()
    #     db.refresh(db_language)
    #     return db_language
    #
    #
    # def add_model(db: Session, model: db_schemas.ModelNameCreate) -> db_schemas.ModelName:
    #     db_model = db_schemas.ModelName(**model.model_dump())
    #     db.add(db_model)
    #     db.commit()
    #     db.refresh(db_model)
    #     return db_model
    #
    #
    # def add_trigger_type(
    #     db: Session, trigger_type: db_schemas.TriggerTypeCreate
    # ) -> db_schemas.TriggerType:
    #     db_trigger_type = db_schemas.TriggerType(**trigger_type.model_dump())
    #     db.add(db_trigger_type)
    #     db.commit()
    #     db.refresh(db_trigger_type)
    #     return db_trigger_type
    #
    #
    # def add_plugin_version(
    #     db: Session, version: db_schemas.PluginVersionCreate
    # ) -> db_schemas.PluginVersion:
    #     db_version = db_schemas.PluginVersion(**version.model_dump())
    #     db.add(db_version)
    #     db.commit()
    #     db.refresh(db_version)
    #     return db_version
    #
    #
    # def add_context(db: Session, context: db_schemas.ContextCreate) -> db_schemas.Context:
    #     db_context = db_schemas.Context(**context.model_dump())
    #     db.add(db_context)
    #     db.commit()
    #     db.refresh(db_context)
    #     return db_context
    #
    #
    # def add_query(db: Session, query: db_schemas.QueryCreate) -> db_schemas.Query:
    #     db_query = db_schemas.Query(**query.model_dump())
    #     db.add(db_query)
    #     db.commit()
    #     db.refresh(db_query)
    #     return db_query
    #
    #
    # def add_telemetry(
    #     db: Session, telemetry: db_schemas.TelemetryCreate
    # ) -> db_schemas.Telemetry:
    #     db_telemetry = db_schemas.Telemetry(**telemetry.model_dump())
    #     db.add(db_telemetry)
    #     db.commit()
    #     db.refresh(db_telemetry)
    #     return db_telemetry
    #
    #
    # def add_ground_truth(
    #     db: Session, ground_truth: db_schemas.GroundTruthCreate
    # ) -> db_schemas.GroundTruth:
    #     db_ground_truth = db_schemas.GroundTruth(**ground_truth.model_dump())
    #     db.add(db_ground_truth)
    #     db.commit()
    #     db.refresh(db_ground_truth)
    #     return db_ground_truth
    #
    #
    # def add_generation(
    #     db: Session, generation: db_schemas.HadGenerationCreate
    # ) -> db_schemas.HadGeneration:
    #     db_generation = db_schemas.HadGeneration(**generation.model_dump())
    #     db.add(db_generation)
    #     db.commit()
    #     db.refresh(db_generation)
    #     return db_generation
    #
    #
    # # UPDATE operations
    # def update_status_of_generation(
    #     self,
    #     query_id: str,
    #     model_id: int,
    #     new_status: db_schemas.HadGenerationUpdate,
    # ) -> bool:
    #     assert is_valid_uuid(query_id)
    #     db_generation = (
    #         db.query(db_schemas.HadGeneration)
    #         .filter(
    #             db_schemas.HadGeneration.query_id == query_id,
    #             db_schemas.HadGeneration.model_id == model_id,
    #         )
    #         .first()
    #     )
    #     try:
    #         if db_generation:
    #             for key, value in new_status.model_dump().items():
    #                 setattr(db_generation, key, value)
    #             db.commit()
    #             return True
    #     except Exception as e:
    #         print(f"Error while updating generation status: {e}")
    #         return False
    #     return False
