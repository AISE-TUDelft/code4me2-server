import json

import pytest
from pydantic import EmailStr
from dotenv import load_dotenv
from hypothesis import given, settings, Verbosity
from hypothesis.strategies import text, none, emails, one_of
from fastapi.testclient import TestClient
from src.backend.main import app
from src.backend.models.Bodies import CreateUser
from pydantic import SecretStr


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, SecretStr):
            return o.get_secret_value()  # Serialize the actual value of SecretStr
        return super().default(o)


class TestCreate:
    @pytest.fixture(scope="class")
    def client(self):
        load_dotenv()
        with TestClient(app) as client:
            yield client

    @settings(max_examples=100, verbosity=Verbosity.verbose)
    @given(
        email=emails(),
        name=text(),
        password=text(),
        googleId=one_of(text(), none()),
        googleCredential=one_of(text(), none()),
    )
    def test_create_simple_user(
        self, client, email, name, password, googleId, googleCredential
    ):
        # Create a CreateUser instance with generated values
        user = CreateUser(
            email=email,
            name=name,
            password=password,
            googleId=googleId,
            googleCredential=googleCredential,
        )
        # Send a POST request with the user data
        response = client.post(
            url="/api/user/create",
            data=json.dumps(user.dict(), cls=CustomJSONEncoder),
            headers={"Content-Type": "application/json"},
        )

        # Assert the response
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
