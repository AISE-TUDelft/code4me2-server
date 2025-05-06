import hashlib
import re


# helper functions
def is_valid_uuid(uuid: str) -> bool:
    if not uuid:
        return False
    uuid = str(uuid)
    uuidv4_pattern = re.compile(
        r"\b[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-4[0-9a-fA-F]{3}\-[89aAbB][0-9a-fA-F]{3}\-[0-9a-fA-F]{12}\b"
    )
    return bool(uuidv4_pattern.fullmatch(uuid))


# Helper function to hash passwords
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()
