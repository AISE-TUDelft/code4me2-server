import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Code4meV2Config(BaseSettings):
    """
    Simplified configuration for database connection
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # -----------------------
    # General & Server Settings
    # -----------------------
    test_mode: bool = Field(alias="TEST_MODE", frozen=True)

    server_version_id: int = Field(alias="SERVER_VERSION_ID", frozen=True)
    server_host: str = Field(alias="SERVER_HOST", frozen=True)
    server_port: int = Field(alias="SERVER_PORT", frozen=True)

    # -----------------------
    # Authentication & Rate Limits
    # -----------------------
    auth_token_expires_in_seconds: int = Field(
        alias="AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS", frozen=True
    )
    session_token_expires_in_seconds: int = Field(
        alias="SESSION_TOKEN_EXPIRES_IN_SECONDS", frozen=True
    )
    token_hook_activation_in_seconds: int = Field(
        alias="TOKEN_HOOK_ACTIVATION_IN_SECONDS", frozen=True
    )
    default_max_request_rate_per_hour: int = Field(
        alias="DEFAULT_MAX_REQUEST_RATE_PER_HOUR", frozen=True
    )
    max_request_rate_per_hour_config: dict = Field(
        alias="MAX_REQUEST_RATE_PER_HOUR_CONFIG", frozen=True
    )

    # -----------------------
    # Database Configuration
    # -----------------------
    db_host: str = Field(alias="DB_HOST", frozen=True)
    db_port: int = Field(alias="DB_PORT", frozen=True)
    db_user: str = Field(alias="DB_USER", frozen=True)
    db_password: str = Field(alias="DB_PASSWORD", frozen=True)
    db_name: str = Field(alias="DB_NAME", frozen=True)

    # -----------------------
    # pgAdmin Configuration
    # -----------------------
    pgadmin_host: str = Field(alias="PGADMIN_HOST", frozen=True)
    pgadmin_port: int = Field(alias="PGADMIN_PORT", frozen=True)
    pgadmin_user: str = Field(alias="PGADMIN_DEFAULT_EMAIL", frozen=True)
    pgadmin_password: str = Field(alias="PGADMIN_DEFAULT_PASSWORD", frozen=True)

    # -----------------------
    # Frontend Configuration
    # -----------------------
    website_host: str = Field(alias="WEBSITE_HOST", frozen=True)
    website_port: int = Field(alias="WEBSITE_PORT", frozen=True)
    react_app_google_client_id: str = Field(
        alias="REACT_APP_GOOGLE_CLIENT_ID", frozen=True
    )

    # -----------------------
    # Redis & Celery Configuration
    # -----------------------
    redis_host: str = Field(alias="REDIS_HOST", frozen=True)
    redis_port: int = Field(alias="REDIS_PORT", frozen=True)
    celery_broker_host: str = Field(alias="CELERY_BROKER_HOST", frozen=True)
    celery_broker_port: int = Field(alias="CELERY_BROKER_PORT", frozen=True)

    # -----------------------
    # Model Configuration
    # -----------------------
    preload_models: bool = Field(alias="PRELOAD_MODELS", frozen=True)
    model_cache_dir: str = Field(
        alias="MODEL_CACHE_DIR", default=os.path.join(".", ".cache"), frozen=True
    )
    model_max_new_tokens: int = Field(
        alias="MODEL_MAX_NEW_TOKENS", default=64, frozen=True
    )
    model_use_cache: bool = Field(alias="MODEL_USE_CACHE", default=True, frozen=True)
    model_num_beams: int = Field(alias="MODEL_NUM_BEAMS", default=1, frozen=True)
    model_use_compile: bool = Field(
        alias="MODEL_USE_COMPILE", default=True, frozen=True
    )
    model_warmup: bool = Field(alias="MODEL_WARMUP", default=True, frozen=True)

    # -----------------------
    # Thread Pool Configuration
    # -----------------------
    thread_pool_max_workers: int = Field(
        alias="THREAD_POOL_MAX_WORKERS", default=2, frozen=True
    )

    # Email configuration
    email_host: str = Field(alias="EMAIL_HOST", frozen=True)
    email_port: int = Field(alias="EMAIL_PORT", frozen=True)
    email_username: str = Field(alias="EMAIL_USERNAME", frozen=True)
    email_password: str = Field(alias="EMAIL_PASSWORD", frozen=True)
    email_use_tls: bool = Field(alias="EMAIL_USE_TLS", frozen=True)
    email_from: str = Field(alias="EMAIL_FROM", frozen=True)
    verification_url: str = Field(alias="VERIFICATION_URL", frozen=True)
