"""Shared BYOA configuration-contract fixture (ISSUE-03 Path A).

A qualified BYOA release must declare how each frozen profile field reaches the
external agent; profiles that set an unmapped field are rejected at creation.
Tests that exercise legitimate BYOA flows use this complete contract.
"""

from __future__ import annotations

BYOA_CONFIG_BINDINGS = [
    {"field": "model", "transport": "env", "key": "BYOA_AGENT_MODEL"},
    {"field": "temperature", "transport": "env", "key": "BYOA_AGENT_TEMPERATURE"},
    {"field": "max_steps", "transport": "env", "key": "BYOA_AGENT_MAX_STEPS"},
    {
        "field": "tools",
        "transport": "env",
        "key": "BYOA_AGENT_TOOLS",
        "format": "csv",
    },
    {"field": "approval_policy", "transport": "env", "key": "BYOA_AGENT_APPROVAL"},
]

#: Runtime bindings a gateway-bound (Goose) release must declare in addition
#: to the profile-field bindings: the plugin fills them at launch.
INFERENCE_GATEWAY_BINDINGS = [
    {"field": "inference_gateway_host", "transport": "env", "key": "BYOA_GATEWAY_HOST"},
    {"field": "inference_gateway_base_path", "transport": "env", "key": "BYOA_GATEWAY_BASE_PATH"},
    {"field": "inference_gateway_credential", "transport": "env", "key": "BYOA_GATEWAY_CREDENTIAL"},
    {
        "field": "provider_kind",
        "transport": "env",
        "key": "BYOA_PROVIDER",
        "value_map": {"openai_compatible": "openai"},
    },
    {"field": "state_dir", "transport": "env", "key": "BYOA_STATE_DIR"},
]

GOOSE_BYOA_CONFIG_BINDINGS = BYOA_CONFIG_BINDINGS + INFERENCE_GATEWAY_BINDINGS
