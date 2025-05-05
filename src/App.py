from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

import Code4meV2Config


class App:
    """
    Database class to manage the database connection and session.
    """

    __db_session_factory = None  # Class-level attribute to store the session factory
    __config = None  # Class-level attribute to store the configuration

    @staticmethod
    def setup(config: Code4meV2Config) -> None:
        """
        Sets up the database engine and session factory.
        """
        database_url = f"postgresql://{config.db_user}:{config.db_password}@{config.db_host}:{str(config.db_port)}/{config.db_name}"
        engine = create_engine(database_url)
        App.__db_session_factory = scoped_session(
            sessionmaker(autocommit=False, autoflush=False, bind=engine)
        )
        App.__config = config

    @staticmethod
    def get_db_session():
        """
        FastAPI-compatible get_db function that yields a session.
        """
        if App.__db_session_factory is None:
            raise RuntimeError(
                "Database is not initialized. Call `App.setup` first in main:app."
            )
        db_session = App.__db_session_factory()
        try:
            yield db_session
        finally:
            db_session.close()

    @staticmethod
    def get_config():
        return App.__config
