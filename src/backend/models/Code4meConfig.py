import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class CodeConfig(BaseSettings):
    """
    Simplified configuration for database connection
    """

    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/code4meV2",
        alias="DATABASE_URL",
    )

    test_database_url: str = Field(
        default="postgresql://postgres:postgres@test_db:5432/code4meV2_test",
        alias="TEST_DATABASE_URL",
    )
