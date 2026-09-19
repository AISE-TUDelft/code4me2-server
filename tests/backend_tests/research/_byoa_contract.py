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
