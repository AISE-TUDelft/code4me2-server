import logging
import time
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker

import Code4meV2Config
from backend.completion import CompletionModels
from backend.session_manager import SessionManager
from database import crud


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
            self.__db_session_factory: scoped_session = None
            self.__config: Code4meV2Config = None
            self.__session_manager: SessionManager = None
            self.__completion_models = None
            self._initialized: bool = True  # Ensure __init__ is only called once

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
        self.__completion_models = CompletionModels()
        if config.preload_models:
            logging.log(logging.INFO, "Preloading llm models...")
            models = crud.get_all_model_names(self.get_db_session())
            for model in models:
                # TODO: Remove the following lines when the code is run on the server with enough disk space since starcoder takes 12GB of memory
                if model.model_name.startswith("bigcode/starcoder"):
                    continue

                logging.log(logging.INFO, f"Loading {model.model_name}...")
                t0 = time.time()
                self.__completion_models.load_model(model.model_name)
                logging.log(
                    logging.INFO,
                    f"{model.model_name} is setup in {time.time()-t0:.2f} seconds",
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

    def get_completion_models(self) -> CompletionModels:
        return self.__completion_models

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

    def cleanup(self):
        self.__session_manager.cleanup()
        self.__db_session_factory.remove()
