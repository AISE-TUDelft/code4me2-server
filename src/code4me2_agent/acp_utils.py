from __future__ import annotations


def capability_value(capabilities: object | None, name: str) -> object | None:
    if capabilities is None:
        return None
    if isinstance(capabilities, dict):
        return capabilities.get(name)
    return getattr(capabilities, name, None)
