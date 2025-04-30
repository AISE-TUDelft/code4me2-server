from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, scoped_session
from .db import Base

from src.backend.models.Code4meConfig import CodeConfig

# Custom get_db function
# def get_db(config: Config):
#
#     engine = create_engine(config.database_url)
#     SessionLocal = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
#
#     db = SessionLocal()
#
#     return db


# TODO: change the function to take config and engine as inputs for testability reasons
def get_db():
    """
    FastAPI-compatible get_db function that yields a session
    """

    config = CodeConfig()

    engine = create_engine(config.database_url)
    SessionLocal = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
