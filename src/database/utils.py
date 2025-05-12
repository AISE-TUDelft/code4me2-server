import re

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

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


def hash_password(password: str) -> str:
    return ph.hash(password)


def verify_password(hashed_password: str, input_password: str) -> bool:
    try:
        return ph.verify(hashed_password, input_password)
    except VerifyMismatchError:
        return False
