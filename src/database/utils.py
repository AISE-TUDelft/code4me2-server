import re
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

ph = PasswordHasher()


# helper functions
def is_valid_uuid(uuid: str) -> bool:
    if not uuid:
        return False
    uuid = str(uuid)
    uuidv4_pattern = re.compile(
        r"\b[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-4[0-9a-fA-F]{3}\-[89aAbB][0-9a-fA-F]{3}\-[0-9a-fA-F]{12}\b"
    )
    return bool(uuidv4_pattern.fullmatch(uuid))


def create_uuid(version: int = 4) -> str:
    if version == 1:
        return str(uuid.uuid1())
    elif version == 3:
        return str(uuid.uuid3(uuid.NAMESPACE_DNS, "example.com"))
    elif version == 4:
        return str(uuid.uuid4())
    elif version == 5:
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, "example.com"))
    else:
        raise ValueError("Invalid UUID version. Supported versions are 1, 3, 4, and 5.")


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(hashed_password: str, input_password: str) -> bool:
    try:
        return ph.verify(hashed_password, input_password)
    except (InvalidHash, VerifyMismatchError):
        return False
