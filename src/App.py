import uuid
from contextlib import contextmanager
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.orm import Session
import Code4meV2Config
from fastapi import Depends, Cookie
import Queries
from backend.session_manager import SessionManager


class App:
    """
    Database class to manage the database connection and session.
    """

    __instance = None  # Class-level attribute to store the singleton instance

    def __new__(cls, *args, **kwargs):
        if cls.__instance is None:
            cls.__instance = super(App, cls).__new__(cls)
        return cls.__instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self.__db_session_factory = None
            self.__config = None
            self.__session_manager = None
            self._initialized = True  # Ensure __init__ is only called once

    def setup(self, config: Code4meV2Config) -> None:
        """
        Sets up the database engine and session factory.
        """
        database_url = f"postgresql://{config.db_user}:{config.db_password}@{config.db_host}:{str(config.db_port)}/{config.db_name}"
        engine = create_engine(database_url)
        self.__db_session_factory = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        self.__config = config
        self.__session_manager = SessionManager(
            host=config.redis_host, port=config.redis_port
        )

    def get_db_session(self) -> Session:
        """
        Provides a database session without requiring a 'with' statement in the calling code.
        """

        @contextmanager
        def __get_db_session_unmanaged():
            """
            FastAPI-compatible get_db function that yields a session.
            """
            session = self.__db_session_factory()
            try:
                yield session
            finally:
                session.close()

        if self.__db_session_factory is None:
            raise RuntimeError("Database is not initialized. Call `App.setup` first.")
        with __get_db_session_unmanaged() as db_session:
            return db_session

    def get_config(self) -> Code4meV2Config:
        return self.__config

    def get_session_manager(self) -> SessionManager:
        return self.__session_manager

    # def get_current_user(self, session_token: Optional[str] = Cookie("session_token")) -> uuid.UUID:
    #     """
    #     Retrieves the current user from the session token.
    #     """
    #     if session_token is None:
    #         return None
    #     session_data = self.__session_manager.get_session(session_token)
    #     if session_data:
    #         return uuid.UUID(session_data["user_id"])
    #     return None

    @classmethod
    def get_instance(cls) -> "App":
        if cls.__instance is None:
            cls.__instance = cls()
        return cls.__instance
