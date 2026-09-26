"""HTTP-level contract tests for the native-agent loopback provider."""

from __future__ import annotations

import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from code4me_e2e.agent_provider import AgentProvider, NativeAgentProvider


class AgentProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = AgentProvider(token="E2E_NATIVE_ANSWER_TEST_MARKER")
        self.provider.start()

    def tearDown(self) -> None:
        self.provider.stop()

    def request(self, route: str, body: object) -> tuple[int, str, str]:
        request = Request(
            self.provider.base_url + route,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer never-record-me"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback fixture
            return response.status, response.headers.get_content_type(), response.read().decode("utf-8")

    def test_chat_completion_json_and_metadata_only_receipt(self) -> None:
        status, content_type, body = self.request("/v1/chat/completions", {
            "model": "goose-model", "stream": False, "messages": [{"role": "user", "content": "private prompt"}],
        })
        self.assertEqual((200, "application/json"), (status, content_type))
        self.assertEqual("E2E_NATIVE_ANSWER_TEST_MARKER", json.loads(body)["choices"][0]["message"]["content"])
        self.assertEqual([{
            "route": "/v1/chat/completions", "model": "goose-model", "stream": False,
            "count": 1, "receipt": 1, "auth_matched": None,
        }], self.provider.requests_since(0))
        self.assertNotIn("private prompt", repr(self.provider.requests))
        self.assertNotIn("never-record-me", repr(self.provider.requests))

    def test_chat_completion_sse_ends_with_done(self) -> None:
        status, content_type, body = self.request("/v1/chat/completions", {"model": "goose-stream", "stream": True})
        self.assertEqual((200, "text/event-stream"), (status, content_type))
        self.assertIn('"content":"E2E_NATIVE_ANSWER_TEST_MARKER"', body)
        self.assertTrue(body.endswith("data: [DONE]\n\n"))
        self.assertEqual("/v1/chat/completions", self.provider.requests_since(0)[0]["route"])
        self.assertTrue(self.provider.requests_since(0)[0]["stream"])

    def test_responses_json_and_sse_are_complete(self) -> None:
        status, content_type, body = self.request("/v1/responses", {"model": "codex-model", "stream": False})
        self.assertEqual((200, "application/json"), (status, content_type))
        self.assertEqual("E2E_NATIVE_ANSWER_TEST_MARKER", json.loads(body)["output"][0]["content"][0]["text"])

        status, content_type, body = self.request("/v1/responses", {"model": "codex-stream", "stream": True})
        self.assertEqual((200, "text/event-stream"), (status, content_type))
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn('"type":"response.completed"', body)
        receipts = self.provider.requests_since(0)
        self.assertEqual(["/v1/responses", "/v1/responses"], [item["route"] for item in receipts])
        self.assertEqual([False, True], [item["stream"] for item in receipts])

    def test_unsupported_and_invalid_requests_fail_without_receipts(self) -> None:
        with self.assertRaises(HTTPError) as unsupported:
            self.request("/v1/embeddings", {"model": "ignored"})
        self.assertEqual(404, unsupported.exception.code)
        self.assertEqual(0, self.provider.request_count())

        malformed = Request(self.provider.base_url + "/v1/responses", data=b"{", method="POST")
        with self.assertRaises(HTTPError) as invalid:
            urlopen(malformed, timeout=5)  # noqa: S310 - loopback fixture
        self.assertEqual(400, invalid.exception.code)
        self.assertEqual(0, self.provider.request_count())

    def test_lifecycle_is_idempotent_and_alias_is_compatible(self) -> None:
        self.assertIs(NativeAgentProvider, AgentProvider)
        port = self.provider.port
        self.assertEqual(port, self.provider.start())
        self.provider.stop()
        self.provider.stop()


if __name__ == "__main__":
    unittest.main()


class GatewayShapeProviderTest(unittest.TestCase):
    """The research inference gateway route, bearer receipts and the 402 refusal."""

    GATEWAY = "/api/research/inference/v1/chat/completions"

    def request(self, provider, route, body, *, bearer):
        request = Request(
            provider.base_url + route,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback fixture
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_gateway_route_answers_and_records_only_whether_the_bearer_matched(self):
        provider = AgentProvider(token="E2E_GATEWAY_MARKER", expected_bearer="c4m-secret-bearer")
        provider.start()
        try:
            status, body = self.request(provider, self.GATEWAY, {"model": "m", "stream": False}, bearer="c4m-secret-bearer")
            self.assertEqual(200, status)
            self.assertEqual("E2E_GATEWAY_MARKER", body["choices"][0]["message"]["content"])
            status, _ = self.request(provider, self.GATEWAY, {"model": "m", "stream": False}, bearer="wrong")
            self.assertEqual(200, status)
            receipts = provider.requests_since(0)
            self.assertEqual([True, False], [item["auth_matched"] for item in receipts])
            self.assertEqual([self.GATEWAY, self.GATEWAY], [item["route"] for item in receipts])
            self.assertNotIn("c4m-secret-bearer", repr(receipts))
        finally:
            provider.stop()

    def test_quota_exhausted_mode_refuses_with_the_gateway_body(self):
        provider = AgentProvider(quota_exhausted=True)
        provider.start()
        try:
            with self.assertRaises(HTTPError) as refused:
                self.request(provider, self.GATEWAY, {"model": "m", "stream": True}, bearer="x")
            self.assertEqual(402, refused.exception.code)
            body = json.loads(refused.exception.read().decode("utf-8"))
            self.assertEqual("insufficient_quota", body["error"]["type"])
            self.assertEqual("quota_exhausted", body["error"]["code"])
            self.assertIn("budget is used up", body["error"]["message"])
            self.assertEqual(1, provider.request_count())
            # A Responses call is unaffected by the chat refusal mode.
            status, _ = self.request(provider, "/v1/responses", {"model": "m", "stream": False}, bearer="x")
            self.assertEqual(200, status)
        finally:
            provider.stop()

    def test_models_listing_stays_unsupported(self):
        provider = AgentProvider()
        provider.start()
        try:
            with self.assertRaises(HTTPError) as unsupported:
                urlopen(Request(provider.base_url + "/v1/models", method="GET"), timeout=5)  # noqa: S310
            self.assertEqual(404, unsupported.exception.code)
        finally:
            provider.stop()
