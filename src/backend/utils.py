import hashlib

from google.auth.transport import requests
from google.oauth2 import id_token


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


# Helper function to hash passwords
def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()
