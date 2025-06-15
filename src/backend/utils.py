import inspect
import json
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Union, get_args, get_origin
from uuid import UUID

from google.auth.transport import requests
from google.oauth2 import id_token
from polyfactory.factories.pydantic_factory import ModelFactory
from polyfactory.field_meta import FieldMeta
from pydantic import BaseModel, EmailStr, SecretStr


def iterable_to_dict(obj: Any, to_json_values: bool = False) -> Dict[str, Any]:
    """
    Recursively converts an iterable (list, tuple, dict, BaseModel) to a plain dictionary.
    Optionally converts output to JSON if to_json_values=True.
    """
    if isinstance(obj, BaseModel):
        return obj.dict()
    elif isinstance(obj, list):
        res = [iterable_to_dict(item, to_json_values) for item in obj]
        return json.dumps(res) if to_json_values else res
    elif isinstance(obj, dict):
        res = {
            key: iterable_to_dict(value, to_json_values) for key, value in obj.items()
        }
        return json.dumps(res) if to_json_values else res
    elif isinstance(obj, tuple):
        return tuple(iterable_to_dict(item) for item in obj)
    elif isinstance(obj, Enum):
        return str(obj.value)
    else:
        return obj


class SerializableBaseModel(BaseModel):
    """
    Base model that customizes dict/json output:
    - Converts EmailStr, SecretStr, UUID, Enum, datetime to str
    - Optionally masks secrets
    - Supports nested serialization via iterable_to_dict
    """

    def dict(
        self,
        exclude_unset=False,
        hide_secrets=False,
        to_json_values=False,
        *args,
        **kwargs
    ) -> Dict[str, Any]:
        data = {}
        for field_name, value in self.__class__.model_fields.items():
            if exclude_unset and getattr(self, field_name) is None:
                continue

            annotation = value.annotation
            field_value = getattr(self, field_name)
            origin = get_origin(annotation)
            args = get_args(annotation)

            def is_type(typ, target):
                try:
                    return typ is target or (
                        isinstance(typ, type) and issubclass(typ, target)
                    )
                except TypeError:
                    return False

            # Unwrap Union types
            if origin is Union:
                non_none_args = [arg for arg in args if arg is not type(None)]
                if non_none_args:
                    annotation = non_none_args[0]

            if is_type(annotation, SecretStr):
                if hide_secrets:
                    data[field_name] = "*" * 8
                else:
                    data[field_name] = (
                        str(field_value.get_secret_value())
                        if field_value is not None
                        else None
                    )
            elif is_type(annotation, EmailStr):
                data[field_name] = str(field_value) if field_value is not None else None
            elif "Enum" in str(type(annotation)):
                data[field_name] = (
                    str(field_value.value) if field_value is not None else None
                )
            elif is_type(annotation, UUID):
                data[field_name] = str(field_value) if field_value is not None else None
            elif is_type(annotation, datetime):
                data[field_name] = (
                    field_value.isoformat() if field_value is not None else None
                )
            else:
                data[field_name] = iterable_to_dict(field_value, to_json_values)

        return data

    def json(self, *args, **kwargs) -> str:
        """Return model as a JSON string."""
        return super().json(*args, **kwargs)

    def __eq__(self, other: Any) -> bool:
        """Equality comparison against another model or dict."""
        if isinstance(other, BaseModel):
            return self == other
        elif isinstance(other, dict):
            return self.dict() == other
        return False

    def __str__(self) -> str:
        """String representation as JSON."""
        return json.dumps(self.dict())


class Fakable:
    """
    Mixin class to add `.fake()` support for generating synthetic data using Polyfactory.
    """

    @classmethod
    def fake(cls, n: int = 1, **overrides) -> Union[BaseModel, list[BaseModel]]:
        """
        Generates one or more fake instances of the model, optionally overriding fields.
        """

        class _Factory(ModelFactory):
            __model__ = cls

            @classmethod
            def get_constrained_field_value(
                cls, annotation, field_meta: FieldMeta, *args, **kwargs
            ):
                # Special handling for common constrained fields
                if annotation is EmailStr:
                    return cls.__faker__.email()
                elif annotation is SecretStr or field_meta.name in {
                    "password",
                    "previous_password",
                }:
                    return SecretStr("ValidPassword123!")

                return super().get_constrained_field_value(
                    annotation, field_meta, *args, **kwargs
                )

            @classmethod
            def get_field_value(cls, field_meta: FieldMeta, *args, **kwargs):
                """
                Override to handle nested BaseModel, list of BaseModel, and secrets.
                """
                annotation = field_meta.annotation
                origin = get_origin(annotation)
                args_ = get_args(annotation)

                # Handle SecretStr or Optional[SecretStr]
                if (
                    field_meta.name in ["password", "previous_password"]
                    or annotation is SecretStr
                    or (origin is Union and SecretStr in args_)
                ):
                    return SecretStr("ValidPassword123!")

                # Handle EmailStr or Optional[EmailStr]
                if (
                    field_meta.name == "email"
                    and annotation is EmailStr
                    or (origin is Union and EmailStr in args_)
                ):
                    return cls.__faker__.email()

                # Handle nested BaseModel
                if inspect.isclass(annotation) and issubclass(annotation, BaseModel):
                    if hasattr(annotation, "fake"):
                        return annotation.fake()

                # Handle List[BaseModel]
                if hasattr(annotation, "__origin__") and annotation.__origin__ is list:
                    inner_type = annotation.__args__[0] if annotation.__args__ else None
                    if (
                        inner_type
                        and inspect.isclass(inner_type)
                        and issubclass(inner_type, BaseModel)
                    ):
                        if hasattr(inner_type, "fake"):
                            count = cls.__faker__.random_int(min=1, max=3)
                            return [inner_type.fake() for _ in range(count)]

                return super().get_field_value(field_meta, *args, **kwargs)

        if n == 1:
            return _Factory.build(**overrides)
        else:
            return _Factory.batch(size=n, **overrides)


def verify_jwt_token(token: str, provider: str = "google"):
    """
    Verifies a JWT token using the provider's public key infrastructure.
    Currently supports Google only.
    """
    try:
        if provider == "google":
            id_info = id_token.verify_oauth2_token(token, requests.Request())
            email = id_info.get("email")
            return {"email": email, "id_info": id_info}
    except ValueError:
        return None


def recursive_json_loads(obj):
    """
    Recursively attempts to JSON-decode any nested strings in the input structure.
    """
    if isinstance(obj, str):
        try:
            loaded = json.loads(obj)
            return recursive_json_loads(loaded)
        except (json.JSONDecodeError, TypeError):
            return obj
    elif isinstance(obj, dict):
        return {k: recursive_json_loads(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [recursive_json_loads(item) for item in obj]
    else:
        return obj
