from abc import ABC
from datetime import datetime
from enum import EnumType
from typing import Union, Dict, Any
from uuid import UUID

from polyfactory.factories.pydantic_factory import ModelFactory
from polyfactory.field_meta import FieldMeta
from pydantic import BaseModel, EmailStr, Field, SecretStr


class SerializableBaseModel(BaseModel):
    """
    A base class that automatically converts SecretStr and EmailStr fields to plain strings
    when calling dict() or json() methods. Any model that extends this will inherit this functionality.
    """

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        data = {}
        # Iterate over the fields and convert fields to plain strings
        for field_name, value in self.model_fields.items():
            if value.annotation is SecretStr:
                data[field_name] = str(getattr(self, field_name).get_secret_value())
            elif value.annotation is EmailStr:
                data[field_name] = str(getattr(self, field_name))
            elif type(value.annotation) == EnumType:
                data[field_name] = str(getattr(self, field_name).value)
            elif value.annotation is UUID:
                data[field_name] = str(getattr(self, field_name))
            elif value.annotation is datetime:
                data[field_name] = getattr(self, field_name).isoformat()
            elif value.annotation is BaseModel:
                data[field_name] = getattr(self, field_name).dict(*args, **kwargs)
            else:
                data[field_name] = getattr(self, field_name)
        return data

    def json(self, *args, **kwargs) -> str:
        # Convert the model to JSON (a string)
        return super().json(*args, **kwargs)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, BaseModel):
            return self == other
        elif isinstance(other, dict):
            return self.dict() == other
        else:
            return False


class Fakable:
    @classmethod
    def fake(cls, n: int = 1, **overrides) -> Union[BaseModel, list[BaseModel]]:
        """
        Generates fake data for the class that calls this method.
        It works dynamically for any subclass of Fakable.
        """

        class _Factory(ModelFactory):
            __model__ = cls

            @classmethod
            def get_constrained_field_value(
                cls, annotation, field_meta: FieldMeta, *args, **kwargs
            ):
                if annotation is EmailStr:
                    return cls.__faker__.email()
                elif annotation is SecretStr:
                    return "ValidPassword123!"
                return super().get_constrained_field_value(
                    annotation, field_meta, *args, **kwargs
                )

        if n == 1:
            return _Factory.build(**overrides)
        else:
            return _Factory.batch(size=n, **overrides)


class ModelBase(SerializableBaseModel, Fakable, ABC):
    pass


class UserBase(ModelBase):
    user_id: UUID = Field(..., description="Unique id for the user")
    email: EmailStr = Field(..., description="User's email address")
    name: str = Field(..., description="User's full name")
    joined_at: datetime = Field(..., description="When the user was created")
    verified: bool = Field(
        ..., description="Whether the user's email has been verified"
    )

    model_config = {
        "from_attributes": True,  # enables reading from ORM objects
        "extra": "ignore",  # Disallow extra fields
    }


# Query
class QueryBase(ModelBase):
    query_id: UUID
    user_id: UUID
    telemetry_id: UUID
    context_id: UUID
    total_serving_time: int
    timestamp: str
    server_version_id: UUID


# Model Name
class ModelNameBase(ModelBase):
    model_name: str

    model_config = {"protected_namespaces": ()}


# Plugin Version
class PluginVersionBase(ModelBase):
    version_name: str
    ide_type: str
    description: str
