"""Upstream LLM provider resolution for the agent inference relay.

Merge decision 6: rather than hardcoding a provider, every agent profile
configures a ``base_url`` + ``api_key_ref`` + ``model`` triple, and the upstream
only has to speak the OpenAI-compatible chat-completions wire format. That is a
superset of what either fork did on its own — a local Ollama install, Groq,
OpenRouter and OpenAI itself all work identically through this path, so
switching provider is a config change rather than a code change.

Two consequences worth being explicit about:

* API **keys are never stored in the database**. A profile stores
  ``api_key_ref``, the *name* of the environment variable to read the key from
  at request time, so dumping the profile table (or exposing it through the
  admin UI) can't leak a credential.
* Codex's proprietary Responses API is not chat-completions-shaped, so it stays
  a special-cased normalization path keyed on ``framework_version == "codex"``
  rather than being the default (see ``agents.normalize``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

# Fallback upstream used when a profile leaves `base_url` unset. Ollama's
# OpenAI-compatible endpoint is the default because it needs no credential and
# no account, so a fresh checkout works offline at zero cost.
DEFAULT_BASE_URL_ENV = "AGENT_UPSTREAM_BASE_URL"
DEFAULT_BASE_URL = "http://localhost:11434/v1"

# Fallback env var consulted for the API key when a profile leaves
# `api_key_ref` unset. Local Ollama ignores the credential entirely, so an
# empty key is a valid, working configuration.
DEFAULT_API_KEY_ENV = "AGENT_UPSTREAM_API_KEY"


@dataclass(frozen=True)
class Upstream:
    """A resolved OpenAI-compatible upstream target."""

    base_url: str
    api_key: str
    model: str
    # Which agent runtime this task belongs to: code4me2-agent | goose | codex.
    framework_version: Optional[str] = None

    @property
    def is_responses_api(self) -> bool:
        """Whether this runtime speaks OpenAI's Responses API rather than
        chat-completions. Only Codex does."""
        return (self.framework_version or "").strip().lower() == "codex"

    def endpoint(self, *, responses_api: bool) -> str:
        """Full URL for the request, given the wire shape actually observed.

        The shape is taken from the request body (``input`` vs ``messages``)
        rather than from ``framework_version`` alone, so a Codex profile driving
        a chat-completions request still lands on the right path.
        """
        suffix = "responses" if responses_api else "chat/completions"
        return f"{self.base_url.rstrip('/')}/{suffix}"


def resolve_api_key(api_key_ref: Optional[str]) -> str:
    """Read the upstream API key out of the environment.

    ``api_key_ref`` is an environment variable *name* taken from the agent
    profile. An unset or empty value resolves to "" rather than raising: local
    providers (Ollama, llama.cpp, vLLM without auth) don't need a credential,
    and failing the request would make the zero-config path unusable.
    """
    env_name = (api_key_ref or "").strip() or DEFAULT_API_KEY_ENV
    key = os.getenv(env_name, "").strip()
    if not key:
        logging.info(
            f"[Agent/provider] no API key in ${env_name} — forwarding unauthenticated "
            f"(fine for a local provider, will 401 against a hosted one)"
        )
    return key


def resolve_base_url(base_url: Optional[str]) -> str:
    """Pick the upstream base URL: profile value, else env override, else Ollama."""
    candidate = (base_url or "").strip()
    if candidate:
        return candidate
    return os.getenv(DEFAULT_BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL


def resolve_upstream(
    *,
    model: str,
    base_url: Optional[str] = None,
    api_key_ref: Optional[str] = None,
    framework_version: Optional[str] = None,
) -> Upstream:
    """Build the upstream target for one inference call.

    Callers pass the values snapshotted onto the ``agent_task`` row at
    task-creation time (not the live profile), so editing a profile mid-run
    never changes where an in-flight task's calls go.
    """
    resolved = Upstream(
        base_url=resolve_base_url(base_url),
        api_key=resolve_api_key(api_key_ref),
        model=model,
        framework_version=framework_version,
    )
    logging.info(
        f"[Agent/provider] upstream={resolved.base_url} model={resolved.model!r} "
        f"runtime={resolved.framework_version or 'unknown'} "
        f"authenticated={bool(resolved.api_key)}"
    )
    return resolved
