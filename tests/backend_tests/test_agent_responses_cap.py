from agents.inference import _cap_responses_output_tokens


def test_caps_codex_default_without_raising_a_smaller_request():
    large = {"max_output_tokens": 65536}
    assert _cap_responses_output_tokens(large, "512") == 512
    assert large["max_output_tokens"] == 512

    small = {"max_output_tokens": 128}
    assert _cap_responses_output_tokens(small, "512") == 128
    assert small["max_output_tokens"] == 128


def test_missing_or_invalid_limit_leaves_request_unchanged():
    for configured in ("", "0", "-1", "not-a-number"):
        body = {"max_output_tokens": 65536}
        assert _cap_responses_output_tokens(body, configured) is None
        assert body["max_output_tokens"] == 65536


def test_cap_fills_missing_or_invalid_request_budget():
    for original in ({}, {"max_output_tokens": None}, {"max_output_tokens": True}):
        body = dict(original)
        assert _cap_responses_output_tokens(body, "512") == 512
        assert body["max_output_tokens"] == 512
