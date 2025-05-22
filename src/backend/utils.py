import inspect
from datetime import datetime
from typing import Any, Dict, Union
from uuid import UUID

from google.auth.transport import requests
from google.oauth2 import id_token
from polyfactory.factories.pydantic_factory import ModelFactory
from polyfactory.field_meta import FieldMeta
from pydantic import BaseModel, EmailStr, SecretStr


def iterable_to_dict(obj: Any) -> Dict[str, Any]:
    """
    Converts an iterable to a dictionary, handling nested models and custom types.
    """
    if isinstance(obj, BaseModel):
        return obj.dict()
    elif isinstance(obj, list):
        return [iterable_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: iterable_to_dict(value) for key, value in obj.items()}
    else:
        return obj


class SerializableBaseModel(BaseModel):
    """
    A base class that automatically converts SecretStr and EmailStr fields to plain strings
    when calling dict() or json() methods. Any model that extends this will inherit this functionality.
    """

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        data = {}
        # Iterate over the fields and convert fields to plain strings
        for field_name, value in self.__class__.model_fields.items():
            if value.annotation is SecretStr:
                data[field_name] = str(getattr(self, field_name).get_secret_value())
            elif value.annotation is EmailStr:
                data[field_name] = str(getattr(self, field_name))
            elif "Enum" in str(type(value.annotation)):
                data[field_name] = str(getattr(self, field_name).value)
            elif value.annotation is UUID:
                data[field_name] = str(getattr(self, field_name))
            elif value.annotation is datetime:
                data[field_name] = getattr(self, field_name).isoformat()
            else:
                data[field_name] = iterable_to_dict(getattr(self, field_name))
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
        It works dynamically for any subclass of Fakable and handles nested BaseModel relationships.
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

            @classmethod
            def get_field_value(cls, field_meta: FieldMeta, *args, **kwargs):
                """Override to handle nested BaseModel objects"""
                annotation = field_meta.annotation

                # Handle nested BaseModel objects
                if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                    if hasattr(annotation, "fake"):
                        return annotation.fake()

                # Handle lists of BaseModel objects
                if hasattr(annotation, "__origin__") and annotation.__origin__ is list:
                    inner_type = annotation.__args__[0] if annotation.__args__ else None
                    if (
                        inner_type
                        and inspect.isclass(inner_type)
                        and issubclass(inner_type, BaseModel)
                    ):
                        if hasattr(inner_type, "fake"):
                            # Generate 1-3 items for lists by default
                            count = cls.__faker__.random_int(min=1, max=3)
                            return [inner_type.fake() for _ in range(count)]

                return super().get_field_value(field_meta, *args, **kwargs)

        if n == 1:
            return _Factory.build(**overrides)
        else:
            return _Factory.batch(size=n, **overrides)


def verify_jwt_token(token: str, provider: str = "google"):
    try:
        if provider == "google":
            # This automatically fetches Google's public keys
            id_info = id_token.verify_oauth2_token(token, requests.Request())

            # Example fields
            email = id_info.get("email")  # user's email
            return {"email": email, "id_info": id_info}
    except ValueError:
        return None
