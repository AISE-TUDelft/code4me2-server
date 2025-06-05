import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Code4meV2Config(BaseSettings):
    """
    Simplified configuration for database connection
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    survey_link: str = Field(alias="SURVEY_LINK", frozen=True)
    session_length: int = Field(alias="SESSION_LENGTH", frozen=True)
    max_failed_session_attempts: int = Field(
        alias="MAX_FAILED_SESSION_ATTEMPTS", frozen=True
    )
    server_version_id: int = Field(alias="SERVER_VERSION_ID", frozen=True)
    max_request_rate: int = Field(
        alias="MAX_REQUEST_RATE", frozen=True
    )  # maximum amount of requests that are allowed per hour per user -> 1000 would indicate 1000 requests per hour
    db_password: str = Field(alias="DB_PASSWORD", frozen=True)
    db_host: str = Field(alias="DB_HOST", frozen=True)
    db_user: str = Field(alias="DB_USER", frozen=True)
    db_name: str = Field(alias="DB_NAME", frozen=True)
    db_port: int = Field(alias="DB_PORT", frozen=True)
    # Extra configs added for pgadmin support
    pgadmin_port: int = Field(alias="PGADMIN_PORT", frozen=True)
    pgadmin_user: str = Field(alias="PGADMIN_DEFAULT_EMAIL", frozen=True)
    pgadmin_password: str = Field(alias="PGADMIN_DEFAULT_PASSWORD", frozen=True)
    # Extra configs added for frontend support
    website_host: str = Field(alias="WEBSITE_HOST", frozen=True)
    website_port: int = Field(alias="WEBSITE_PORT", frozen=True)

    redis_host: str = Field(alias="REDIS_HOST", frozen=True)
    redis_port: int = Field(alias="REDIS_PORT", frozen=True)
    celery_broker_host: str = Field(alias="CELERY_BROKER_HOST", frozen=True)
    celery_broker_port: int = Field(alias="CELERY_BROKER_PORT", frozen=True)

    backend_host: str = Field(alias="REACT_APP_BACKEND_HOST", frozen=True)
    backend_port: int = Field(alias="REACT_APP_BACKEND_PORT", frozen=True)
    react_app_google_client_id: str = Field(
        alias="REACT_APP_GOOGLE_CLIENT_ID", frozen=True
    )

    auth_token_expires_in_seconds: int = Field(
        alias="AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS", frozen=True
    )
    session_token_expires_in_seconds: int = Field(
        alias="SESSION_TOKEN_EXPIRES_IN_SECONDS", frozen=True
    )

    preload_models: bool = Field(alias="PRELOAD_MODELS", frozen=True)
    debug_mode: bool = Field(alias="DEBUG_MODE", frozen=True)
    test_mode: bool = Field(alias="TEST_MODE", frozen=True)

    # Model configuration parameters
    model_cache_dir: str = Field(
        alias="MODEL_CACHE_DIR", default=os.path.join(".", ".cache"), frozen=True
    )
    model_max_new_tokens: int = Field(
        alias="MODEL_MAX_NEW_TOKENS", default=128, frozen=True
    )
    model_use_cache: bool = Field(alias="MODEL_USE_CACHE", default=True, frozen=True)
    model_num_beams: int = Field(alias="MODEL_NUM_BEAMS", default=1, frozen=True)
    model_use_compile: bool = Field(
        alias="MODEL_USE_COMPILE", default=False, frozen=True
    )
    model_warmup: bool = Field(alias="MODEL_WARMUP", default=False, frozen=True)

    # Thread pool configuration
    thread_pool_max_workers: int = Field(
        alias="THREAD_POOL_MAX_WORKERS", default=5, frozen=True
    )
