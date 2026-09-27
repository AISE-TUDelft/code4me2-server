"""Live end-to-end check: classic completion and chat answered by a provider.

A freshly seeded database must serve the plugin's default classic rows (id 1
completion, id 3 chat) through OpenRouter, while the other rows stay local and are
only loaded when explicitly chosen. The test runs on its own disposable stack
(project ``code4me-e2e-classic``), never the developer stack, and makes a few real,
cheap OpenRouter calls, so it is opt-in:

    cd code4me2-server/e2e
    E2E_CLASSIC_PROVIDER=1 python3 -m unittest tests.test_classic_provider_e2e -v

OPENROUTER_API_KEY is taken from the environment or ``code4me2-server/.env`` and is
never printed. Set E2E_KEEP_STACK=1 to leave the stack running for inspection.
"""
import json
import os
import unittest
import uuid
from pathlib import Path

from code4me_e2e import stack
from code4me_e2e.config import load_scenario
from code4me_e2e.http import HttpClient, HttpResponse

KEY_NAME = "OPENROUTER_API_KEY"
SERVER_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_PROVIDER_ROWS = {1: "completion", 3: "chat"}
# Seeded ids (plugin version, "manual" trigger, a language); the plugin sends the same shape.
TELEMETRY = {"version_id": 1, "trigger_type_id": 1, "language_id": 1}
STORE_FLAGS = {"store_context": True, "store_contextual_telemetry": True,
               "store_behavioral_telemetry": True}


def openrouter_key() -> str:
    """The key from the environment, else from code4me2-server/.env; never printed."""
    key = os.environ.get(KEY_NAME, "").strip()
    if not key and SERVER_ENV_FILE.is_file():
        for line in SERVER_ENV_FILE.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == KEY_NAME:
                key = value.strip().strip("\"'")
    return key


def expect(response: HttpResponse, status: int, what: str):
    if response.status != status:
        raise AssertionError(f"{what}: HTTP {response.status}: {response.text[:300]}")
    return response.json


@unittest.skipUnless(
    os.environ.get("E2E_CLASSIC_PROVIDER") == "1",
    "live test (Docker stack + real OpenRouter calls): set E2E_CLASSIC_PROVIDER=1",
)
class ClassicProviderEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        key = openrouter_key()
        if not key:
            raise unittest.SkipTest(f"{KEY_NAME} is not set in the environment or {SERVER_ENV_FILE}")
        # Compose interpolation hands the key to the disposable backend only.
        os.environ[KEY_NAME] = key
        cls.scenario = load_scenario(
            overrides=[
                "stack.project_name=code4me-e2e-classic",
                "stack.backend_port=28108",
                "stack.db_port=25532",
                "stack.redis_port=26479",
                "stack.stub_port=29099",
            ]
        )
        stack.up(cls.scenario)
        stack.wait_until_ready(cls.scenario)
        cls.client = cls.plugin_like_client()

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("E2E_KEEP_STACK") != "1":
            stack.down(cls.scenario)

    @classmethod
    def plugin_like_client(cls) -> HttpClient:
        """A signed-in user with a session and an active project, as the plugin has."""
        account = cls.scenario.participant
        client = HttpClient(cls.scenario.base_url, label="classic", timeout=90.0)
        config_id = int(stack.psql(cls.scenario, "SELECT config_id FROM config ORDER BY config_id LIMIT 1;"))
        created = client.post(
            "/api/user/create",
            {"email": account.email, "name": account.name, "password": account.password,
             "config_id": config_id},
        )
        if created.status not in (201, 409):
            raise AssertionError(f"creating the user: HTTP {created.status}: {created.text[:300]}")
        expect(client.post("/api/user/authenticate",
                           {"email": account.email, "password": account.password}), 200, "login")
        expect(client.get("/api/session/acquire"), 200, "session acquire")
        project = expect(client.post("/api/project/create", {"project_name": "classic-e2e"}),
                         201, "project create")
        expect(client.put("/api/project/activate", {"project_id": project["project_token"]}),
               200, "project activate")
        return client

    def assert_served_by_provider(self, kind: str) -> None:
        logs = stack.backend_logs(self.scenario, tail=500)
        self.assertIn(f":provider:{kind}:", logs, "the provider-backed row was not used")
        self.assertIn("openrouter.ai/api/v1/chat/completions", logs)
        self.assertNotIn("Loading model with cache directory", logs, "a local model was loaded")

    def test_default_rows_are_provider_backed_and_the_rest_stay_local(self):
        rows = stack.psql(self.scenario, "SELECT model_id, model_parameters FROM model_name ORDER BY model_id;")
        params = {int(model_id): json.loads(raw) for model_id, raw in
                  (line.split("|", 1) for line in rows.splitlines())}
        for model_id, kind in DEFAULT_PROVIDER_ROWS.items():
            self.assertEqual(params[model_id].get("provider"), "openai_compatible", params[model_id])
            self.assertEqual(params[model_id].get("kind"), kind)
            self.assertEqual(params[model_id].get("api_key_ref"), KEY_NAME)
        local = [model_id for model_id, row in params.items() if "provider" not in row]
        self.assertEqual(sorted(local), sorted(set(params) - set(DEFAULT_PROVIDER_ROWS)))

    def test_completion_is_answered_by_the_provider(self):
        body = expect(self.client.post("/api/completion/request", {
            "model_ids": [1],
            "context": {"prefix": 'def fibonacci(n):\n    """Return the n-th Fibonacci number."""\n',
                        "suffix": "\n\nprint(fibonacci(10))\n", "file_name": "fib.py"},
            "contextual_telemetry": TELEMETRY,
            "behavioral_telemetry": {},
            **STORE_FLAGS,
        }), 200, "completion request")

        [item] = body["data"]["completions"]
        self.assertEqual(item.get("model_id"), 1, item)
        self.assertTrue(item["completion"].strip(), item)
        self.assert_served_by_provider("completion")

    def test_chat_is_answered_by_the_provider(self):
        body = expect(self.client.post("/api/chat/request", {
            "model_ids": [3],
            "chat_id": str(uuid.uuid4()),
            "messages": [["user", "In one short sentence, what is a Python list comprehension?"]],
            "context": {"prefix": "", "suffix": "", "file_name": "scratch.py"},
            "contextual_telemetry": TELEMETRY,
            "behavioral_telemetry": {},
            "web_enabled": False,
            **STORE_FLAGS,
        }), 200, "chat request")

        [answer] = body["history"][0]["assistant_responses"]
        self.assertEqual(answer.get("model_id"), 3, answer)
        self.assertTrue(answer["completion"].strip(), answer)
        self.assert_served_by_provider("chat")


if __name__ == "__main__":
    unittest.main()
