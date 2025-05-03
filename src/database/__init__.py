from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from backend.utils import Code4meV2Config
from sqlalchemy.orm import Session


class Database:
    """
    Database class to manage the database connection and session.
    """

    db = None  # Class-level attribute to store the session factory

    @staticmethod
    def setup(config: Code4meV2Config) -> None:
        """
        Sets up the database engine and session factory.
        """
        database_url = f"postgresql://{config.db_user}:{config.db_password}@{config.db_host}:{config.db_port}/{config.db_name}"
        engine = create_engine(database_url)
        Database.db = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )

    @staticmethod
    def get_db_session():
        """
        FastAPI-compatible get_db function that yields a session.
        """
        if Database.db is None:
            raise RuntimeError(
                "Database is not initialized. Call `Database.setup` first in main:app."
            )
        db = Database.db()
        try:
            yield db
        finally:
            db.close()
