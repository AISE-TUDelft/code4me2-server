"""
Application singleton class for Code4meV2.

This module contains the main App class that manages database connections,
Redis sessions, Celery broker, and completion models using the singleton pattern.
"""

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
    Singleton application class to manage database connections and application services.

    This class serves as the main application container that manages:
    - Database connection and session factory
    - Redis manager for session handling
    - Celery broker for background tasks
    - Completion models for AI functionality

    The class implements the singleton pattern to ensure only one instance exists
    throughout the application lifecycle.
    """

    __instance = None  # Class-level attribute to store the singleton instance

    def __new__(cls, *args, **kwargs):
        """
        Create or return the singleton instance.

        Implements the singleton pattern by ensuring only one instance
        of the App class can exist at any time.

        Returns:
            App: The singleton instance of the App class
        """
        if cls.__instance is None:
            cls.__instance = super(App, cls).__new__(cls)
        return cls.__instance

    def __init__(self):
        """
        Initialize the App instance.

        Sets up all private attributes and calls the setup method with default configuration.
        Uses a flag to ensure initialization only happens once for the singleton instance.
        """
        if not hasattr(self, "_initialized"):
            # Initialize all private attributes to None with type hints
            self.__db_session_factory: scoped_session = None  # type: ignore
            self.__config: Code4meV2Config = None  # type: ignore
            self.__redis_manager: RedisManager = None  # type: ignore
            self.__celery_broker: CeleryBroker = None  # type: ignore
            self.__completion_models: CompletionModels = None  # type: ignore

            # Flag to ensure __init__ is only called once for singleton
            self._initialized: bool = True

            # Setup the application with default configuration
            self.__setup(Code4meV2Config())  # type: ignore

    def __setup(self, config: Code4meV2Config) -> None:
        """
        Sets up all application services and connections.

        This method initializes:
        1. Database engine and session factory
        2. Configuration storage
        3. Celery broker for background tasks
        4. Redis manager for session handling
        5. Completion models for AI functionality
        6. Background thread for session expiration monitoring
        7. Model preloading if configured

        Args:
            config (Code4meV2Config): Configuration object containing all settings

        Raises:
            RuntimeError: If database connection fails
        """
        # Build PostgreSQL database URL from configuration
        database_url = f"postgresql://{config.db_user}:{config.db_password}@{config.db_host}:{str(config.db_port)}/{config.db_name}"

        # Create SQLAlchemy engine for database connections
        engine = create_engine(database_url)

        # Create scoped session factory for thread-safe database operations
        self.__db_session_factory = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )

        # Test database connectivity by executing a simple query
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

        # Store configuration for later access
        self.__config = config

        # Initialize Celery broker for background task processing
        self.__celery_broker = CeleryBroker(
            host=config.celery_broker_host,
            port=config.celery_broker_port,
        )

        # Initialize Redis manager for session and token management
        self.__redis_manager = RedisManager(
            host=config.redis_host,
            port=config.redis_port,
            auth_token_expires_in_seconds=config.auth_token_expires_in_seconds,
            session_token_expires_in_seconds=config.session_token_expires_in_seconds,
            email_verification_token_expires_in_seconds=config.email_verification_token_expires_in_seconds,
            reset_password_token_expires_in_seconds=config.reset_password_token_expires_in_seconds,
            token_hook_activation_in_seconds=config.token_hook_activation_in_seconds,
        )

        # Start background thread to listen for Redis key expiration events
        # This handles cleanup of expired sessions in the database
        try:
            session_expiration_listener_thread = threading.Thread(
                target=self.__redis_manager.listen_for_expired_keys,
                args=(self.__db_session_factory(),),
                daemon=True,  # Thread will exit when main program exits
            )
            session_expiration_listener_thread.start()
        except Exception as e:
            logging.log(
                logging.ERROR, f"Exception happened in session expiration listener: {e}"
            )

        # Initialize completion models for AI functionality
        self.__completion_models = CompletionModels(config=config)

        # Preload models if configured to do so for faster response times
        if config.preload_models:
            logging.log(logging.INFO, "Preloading llm models...")

            # Get all available model names from database
            models = crud.get_all_model_names(self.get_db_session())

            # Load each model and log the time taken
            for model in models:
                logging.log(logging.INFO, f"Loading {model.model_name}...")
                t0 = time.time()  # Start timing
                self.__completion_models.load_model(str(model.model_name))
                loading_time = time.time() - t0  # Calculate loading time
                logging.log(
                    logging.INFO,
                    f"{model.model_name} is setup in {loading_time:.2f} seconds",
                )

    def get_db_session(self) -> Session:
        """
        Provides a managed database session with automatic transaction handling.

        This method returns a database session that automatically commits on success
        and rolls back on exceptions. The session is automatically closed after use.

        Returns:
            Session: SQLAlchemy database session with transaction management

        Raises:
            RuntimeError: If database is not initialized
        """

        @contextmanager
        def __get_db_session_unmanaged():
            """
            Internal context manager for database session lifecycle.

            FastAPI-compatible get_db function that yields a session with
            automatic commit/rollback and cleanup handling.

            Yields:
                Session: Database session for operations
            """
            session = self.__db_session_factory()
            try:
                yield session
                session.commit()  # Commit transaction on success
            except Exception:
                session.rollback()  # Rollback on any exception
                raise  # Re-raise the exception
            finally:
                session.close()  # Always close the session

        # Check if database is initialized before providing session
        if self.__db_session_factory is None:
            raise RuntimeError("Database is not initialized. Call `App.setup` first.")

        # Use the context manager to get and return a managed session
        with __get_db_session_unmanaged() as db_session:
            return db_session

    def get_db_session_fresh(self):
        """
        Returns a new unmanaged database session.

        This method provides a fresh database session without automatic
        transaction management. The caller is responsible for committing,
        rolling back, and closing the session.

        Returns:
            Session: New SQLAlchemy database session

        Raises:
            RuntimeError: If database is not initialized

        Note:
            Caller must handle session lifecycle (commit/rollback/close)
            or use it within a 'with' statement for proper cleanup.
        """
        if self.__db_session_factory is None:
            raise RuntimeError("Database is not initialized. Call `App.setup` first.")
        return self.__db_session_factory()

    def get_config(self) -> Code4meV2Config:
        """
        Get the application configuration object.

        Returns:
            Code4meV2Config: The configuration object used to initialize the app
        """
        return self.__config

    def get_redis_manager(self) -> RedisManager:
        """
        Get the Redis manager instance.

        Returns:
            RedisManager: Redis manager for session and token operations
        """
        return self.__redis_manager

    def get_completion_models(self) -> CompletionModels:
        """
        Get the completion models manager.

        Returns:
            CompletionModels: Manager for AI completion model operations
        """
        return self.__completion_models

    def get_celery_broker(self) -> CeleryBroker:
        """
        Get the Celery broker instance.

        Returns:
            CeleryBroker: Celery broker for background task management
        """
        return self.__celery_broker

    @classmethod
    def get_instance(cls) -> "App":
        """
        Get the singleton instance of the App class.

        This class method provides access to the singleton instance,
        creating it if it doesn't exist yet.

        Returns:
            App: The singleton App instance
        """
        if cls.__instance is None:
            cls.__instance = cls()
        return cls.__instance

    def cleanup(self):
        """
        Clean up all application resources and connections.

        This method should be called when shutting down the application
        to ensure proper cleanup of:
        - Redis connections and sessions
        - Celery broker connections
        - Database session factory and connections

        Note:
            This method performs cleanup in the reverse order of initialization
            to ensure dependencies are properly handled.
        """
        # Clean up Redis manager with a database session for final operations
        self.__redis_manager.cleanup(db_session=self.__db_session_factory())

        # Clean up Celery broker connections
        self.__celery_broker.cleanup()

        # Remove all database sessions and close connections
        self.__db_session_factory.remove()
