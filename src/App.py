import logging
import threading
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker

from backend.celery_broker import CeleryBroker
from backend.completion import CompletionModels
from backend.redis_manager import RedisManager
from Code4meV2Config import Code4meV2Config
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
            self.__db_session_factory: scoped_session = None  # type: ignore
            self.__config: Code4meV2Config = None  # type: ignore
            self.__redis_manager: RedisManager = None  # type: ignore
            self.__celery_broker: CeleryBroker = None  # type: ignore
            self.__completion_models: CompletionModels = None  # type: ignore
            self._initialized: bool = True  # Ensure __init__ is only called once
            self.__setup(Code4meV2Config())  # type: ignore

    def __setup(self, config: Code4meV2Config) -> None:
        """
        Sets up the database engine and session factory.
        """
        database_url = f"postgresql://{config.db_user}:{config.db_password}@{config.db_host}:{str(config.db_port)}/{config.db_name}"
        engine = create_engine(database_url)
        self.__db_session_factory = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        # Check if the database allows requests
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))  # Properly wrapped SQL statement
                logging.log(
                    logging.INFO, f"Connected to database {database_url} successfully."
                )
        except Exception as e:
            logging.log(logging.ERROR, f"Failed to connect to the database: {e}")
            raise RuntimeError(
                f"Database {database_url} is not accessible. Please check the configuration."
            )

        self.__config = config

        self.__celery_broker = CeleryBroker(
            host=config.celery_broker_host,
            port=config.celery_broker_port,
        )

        self.__redis_manager = RedisManager(
            host=config.redis_host,
            port=config.redis_port,
            auth_token_expires_in_seconds=config.auth_token_expires_in_seconds,
            session_token_expires_in_seconds=config.session_token_expires_in_seconds,
            token_hook_activation_in_seconds=config.token_hook_activation_in_seconds,
            # TODO: Set whether to store multi file context on db or not
        )

        try:
            session_expiration_listener_thread = threading.Thread(
                target=self.__redis_manager.listen_for_expired_keys,
                args=(self.__db_session_factory(),),
                daemon=True,
            )
            session_expiration_listener_thread.start()
        except Exception as e:
            logging.log(
                logging.ERROR, f"Exception happened in session expiration listener: {e}"
            )

        self.__completion_models = CompletionModels()
        if config.preload_models:
            logging.log(logging.INFO, "Preloading llm models...")
            models = crud.get_all_model_names(self.get_db_session())
            for model in models:
                logging.log(logging.INFO, f"Loading {model.model_name}...")
                t0 = time.time()
                self.__completion_models.load_model(
                    str(model.model_name), config=config
                )
                logging.log(
                    logging.INFO,
                    f"{model.model_name} is setup in {time.time() - t0:.2f} seconds",
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
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        if self.__db_session_factory is None:
            raise RuntimeError("Database is not initialized. Call `App.setup` first.")
        with __get_db_session_unmanaged() as db_session:
            return db_session

    def get_db_session_fresh(self):
        """
        Returns a new database session.
        Caller is responsible for closing or using it with 'with' statement.
        """
        if self.__db_session_factory is None:
            raise RuntimeError("Database is not initialized. Call `App.setup` first.")
        return self.__db_session_factory()

    def get_config(self) -> Code4meV2Config:
        return self.__config

    def get_redis_manager(self) -> RedisManager:
        return self.__redis_manager

    def get_completion_models(self) -> CompletionModels:
        return self.__completion_models

    def get_celery_broker(self) -> CeleryBroker:
        return self.__celery_broker

    @classmethod
    def get_instance(cls) -> "App":
        if cls.__instance is None:
            cls.__instance = cls()
        return cls.__instance

    def cleanup(self):
        self.__redis_manager.cleanup(db_session=self.__db_session_factory())
        self.__celery_broker.cleanup()
        self.__db_session_factory.remove()
