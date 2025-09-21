#!/usr/bin/env python3
"""
Configuration Management for Code4Me V2 Application

This module provides a comprehensive configuration class that manages all
application settings through environment variables and .env files using
Pydantic settings for validation and type safety.

Environment Variables:
    All configuration values are loaded from environment variables or .env file.
    See individual field documentation for specific variable names.

Usage:
    from Code4meV2Config import Code4meV2Config

    config = Code4meV2Config()
    print(f"Server running on {config.server_host}:{config.server_port}")

Author: Your Name
Version: 1.0.0
"""

import os
from typing import Dict, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Code4meV2Config(BaseSettings):
    """
    Configuration class for Code4Me V2 application.

    This class manages all application configuration through environment variables,
    providing type validation, default values, and comprehensive documentation
    for each setting.

    All fields are frozen to prevent accidental modification after initialization.
    Configuration is loaded from environment variables or .env file.

    Attributes:
        model_config: Pydantic settings configuration
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=True
    )

    # -----------------------
    # General & Server Settings
    # -----------------------

    server_version_id: int = Field(
        alias="SERVER_VERSION_ID",
        frozen=True,
        ge=1,
        description="Version identifier for the server instance",
    )

    server_host: str = Field(
        alias="SERVER_HOST",
        frozen=True,
        min_length=1,
        description="Host address where the server will bind (e.g., '0.0.0.0', 'localhost')",
    )

    server_port: int = Field(
        alias="SERVER_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="Port number where the server will listen",
    )

    # -----------------------
    # Authentication & Rate Limits
    # -----------------------

    auth_token_expires_in_seconds: int = Field(
        alias="AUTHENTICATION_TOKEN_EXPIRES_IN_SECONDS",
        frozen=True,
        ge=1,
        description="Expiration time for authentication tokens in seconds",
    )

    session_token_expires_in_seconds: int = Field(
        alias="SESSION_TOKEN_EXPIRES_IN_SECONDS",
        frozen=True,
        ge=1,
        description="Expiration time for session tokens in seconds",
    )
    email_verification_token_expires_in_seconds: int = Field(
        alias="EMAIL_VERIFICATION_TOKEN_EXPIRES_IN_SECONDS",
        frozen=True,
        ge=1,
        description="Expiration time for email verification tokens in seconds",
    )
    reset_password_token_expires_in_seconds: int = Field(
        alias="RESET_PASSWORD_TOKEN_EXPIRES_IN_SECONDS",
        frozen=True,
        ge=1,
        description="Expiration time for reset password tokens in seconds",
    )

    token_hook_activation_in_seconds: int = Field(
        alias="TOKEN_HOOK_ACTIVATION_IN_SECONDS",
        frozen=True,
        ge=1,
        description="Time in seconds for token hook activation",
    )

    default_max_request_rate_per_hour: int = Field(
        alias="DEFAULT_MAX_REQUEST_RATE_PER_HOUR",
        frozen=True,
        ge=1,
        description="Default maximum number of requests allowed per hour per client",
    )

    max_request_rate_per_hour_config: Dict[str, int] = Field(
        alias="MAX_REQUEST_RATE_PER_HOUR_CONFIG",
        frozen=True,
        default_factory=dict,
        description="Dictionary mapping endpoints to their specific rate limits",
    )

    # -----------------------
    # Database Configuration
    # -----------------------

    db_host: str = Field(
        alias="DB_HOST",
        frozen=True,
        min_length=1,
        description="Database server hostname or IP address",
    )

    db_port: int = Field(
        alias="DB_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="Database server port number",
    )

    db_user: str = Field(
        alias="DB_USER",
        frozen=True,
        min_length=1,
        description="Database username for authentication",
    )

    db_password: str = Field(
        alias="DB_PASSWORD",
        frozen=True,
        min_length=1,
        description="Database password for authentication",
    )

    db_name: str = Field(
        alias="DB_NAME",
        frozen=True,
        min_length=1,
        description="Name of the database to connect to",
    )

    db_pool_size: int = Field(
        alias="DB_POOL_SIZE", frozen=True, description="Database connection pool size"
    )
    db_max_overflow: int = Field(
        alias="DB_MAX_OVERFLOW",
        frozen=True,
        description="Maximum number of connections to create beyond the pool size",
    )
    db_pool_timeout: int = Field(
        alias="DB_POOL_TIMEOUT",
        frozen=True,
        ge=1,
        description="Database connection timeout in seconds",
    )
    db_pool_recycle: int = Field(
        alias="DB_POOL_RECYCLE",
        frozen=True,
        ge=1,
        description="Time in seconds after which a connection is recycled",
    )

    # -----------------------
    # pgAdmin Configuration
    # -----------------------

    pgadmin_host: str = Field(
        alias="PGADMIN_HOST",
        frozen=True,
        min_length=1,
        description="pgAdmin server hostname or IP address",
    )

    pgadmin_port: int = Field(
        alias="PGADMIN_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="pgAdmin server port number",
    )

    pgadmin_user: str = Field(
        alias="PGADMIN_DEFAULT_EMAIL",
        frozen=True,
        min_length=1,
        description="Default pgAdmin user email address",
    )

    pgadmin_password: str = Field(
        alias="PGADMIN_DEFAULT_PASSWORD",
        frozen=True,
        min_length=1,
        description="Default pgAdmin user password",
    )

    # -----------------------
    # Frontend Configuration
    # -----------------------

    website_host: str = Field(
        alias="WEBSITE_HOST",
        frozen=True,
        min_length=1,
        description="Frontend website hostname or IP address",
    )

    website_port: int = Field(
        alias="WEBSITE_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="Frontend website port number",
    )

    react_app_google_client_id: str = Field(
        alias="REACT_APP_GOOGLE_CLIENT_ID",
        frozen=True,
        min_length=1,
        description="Google OAuth client ID for React application",
    )

    # -----------------------
    # Redis & Celery Configuration
    # -----------------------

    redis_host: str = Field(
        alias="REDIS_HOST",
        frozen=True,
        min_length=1,
        description="Redis server hostname or IP address",
    )

    redis_port: int = Field(
        alias="REDIS_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="Redis server port number",
    )

    celery_broker_host: str = Field(
        alias="CELERY_BROKER_HOST",
        frozen=True,
        min_length=1,
        description="Celery broker (Redis) hostname or IP address",
    )

    celery_broker_port: int = Field(
        alias="CELERY_BROKER_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="Celery broker (Redis) port number",
    )

    # -----------------------
    # Model Configuration
    # -----------------------

    preload_models: bool = Field(
        alias="PRELOAD_MODELS",
        frozen=True,
        description="Whether to preload ML models at startup for better performance",
    )

    model_cache_dir: str = Field(
        alias="MODEL_CACHE_DIR",
        default=os.path.join(".", ".cache"),
        frozen=True,
        description="Directory path for caching downloaded models",
    )

    model_use_cache: bool = Field(
        alias="MODEL_USE_CACHE",
        default=True,
        frozen=True,
        description="Enable caching for model predictions to improve performance",
    )

    model_use_compile: bool = Field(
        alias="MODEL_USE_COMPILE",
        default=True,
        frozen=True,
        description="Enable model compilation for optimization (PyTorch 2.0+)",
    )

    model_warmup: bool = Field(
        alias="MODEL_WARMUP",
        default=True,
        frozen=True,
        description="Perform model warmup runs to optimize performance",
    )

    # -----------------------
    # Thread Pool Configuration
    # -----------------------

    thread_pool_max_workers: int = Field(
        alias="THREAD_POOL_MAX_WORKERS",
        default=2,
        frozen=True,
        ge=1,
        le=32,
        description="Maximum number of worker threads in the thread pool",
    )

    # -----------------------
    # Email Configuration
    # -----------------------

    email_host: str = Field(
        alias="EMAIL_HOST",
        frozen=True,
        min_length=1,
        description="SMTP server hostname for sending emails",
    )

    email_port: int = Field(
        alias="EMAIL_PORT",
        frozen=True,
        ge=1,
        le=65535,
        description="SMTP server port number (commonly 587 for TLS, 465 for SSL)",
    )

    email_username: str = Field(
        alias="EMAIL_USERNAME",
        frozen=True,
        min_length=1,
        description="Username for SMTP server authentication",
    )

    email_password: str = Field(
        alias="EMAIL_PASSWORD",
        frozen=True,
        min_length=1,
        description="Password for SMTP server authentication",
    )

    email_use_tls: bool = Field(
        alias="EMAIL_USE_TLS",
        frozen=True,
        description="Enable TLS encryption for SMTP connection",
    )

    email_from: str = Field(
        alias="EMAIL_FROM",
        frozen=True,
        min_length=1,
        description="Default 'From' email address for outgoing emails",
    )

    verification_url: str = Field(
        alias="VERIFICATION_URL",
        frozen=True,
        min_length=1,
        description="Base URL for email verification links",
    )
    reset_password_url: str = Field(
        alias="RESET_PASSWORD_URL",
        frozen=True,
        min_length=1,
        description="Base URL for reset password links",
    )

    def __repr__(self) -> str:
        """Return a string representation of the configuration."""
        return f"Code4meV2Config(server={self.server_host}:{self.server_port})"
