"""seed default agent profiles

Without at least one *active* profile, POST /api/agent/task returns 503 ("No
active agent profiles are configured") and a freshly-migrated database cannot
mint tasks at all. This seeds one profile per supported runtime so the system
is usable out of the box.

Only the built-in ``code4me2-agent`` profile is seeded active, because it is the
one runtime that needs no external binary: it points at a local Ollama endpoint,
which costs nothing and needs no API key. The Goose and Codex profiles are
seeded *inactive* — they require a Goose binary / Node toolchain on the
developer's machine, so an operator flips them on from the admin UI once those
prerequisites exist. Inactive profiles are never drawn as A/B arms.

No API keys are stored here (or anywhere in the DB): ``api_key_ref`` names the
environment variable to read the key from at request time.

Revision ID: e2b3c4d5f6a8
Revises: d1a2b3c4e5f7
Create Date: 2026-09-06

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e2b3c4d5f6a8"
down_revision = "d1a2b3c4e5f7"
branch_labels = None
depends_on = None

# Fixed ids so the seeded profiles are identical across all environments.
_PROFILES = [
    {
        "profile_id": "7e1f20ce-fc5c-4ba2-b908-eaf77047d4b4",
        "name": "default-code4me2-agent",
        "framework_version": "code4me2-agent",
        # Ollama speaks the OpenAI-compatible chat-completions format, so the
        # generic provider path works against it unchanged.
        "base_url": "http://localhost:11434/v1",
        "api_key_ref": "OLLAMA_API_KEY",
        "model": "qwen2.5-coder:7b",
        "tools_json": (
            '["read_file", "write_file", "create_file", "replace_text", '
            '"list_files", "search_files", "run_command"]'
        ),
        "approval_policy": "per_step",
        "max_steps": 8,
        "is_active": "true",
    },
    {
        "profile_id": "2b9d7c41-5a30-4c8e-9f21-1d6b0c3a7e58",
        "name": "default-goose",
        "framework_version": "goose",
        "base_url": None,
        "api_key_ref": "AGENT_UPSTREAM_API_KEY",
        "model": "openai/gpt-oss-120b",
        # Empty allowlist = the proxy forwards no tools. Populate from
        # GET /api/agent/available-tools once a run has shown which tools the
        # installed Goose build actually advertises.
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 15,
        "is_active": "false",
    },
    {
        "profile_id": "6c4a8e12-93bf-4d57-8a0e-7f2c5b1d94a3",
        "name": "default-codex",
        "framework_version": "codex",
        "base_url": "https://api.openai.com/v1",
        "api_key_ref": "OPENAI_API_KEY",
        "model": "gpt-5.1-codex-mini",
        # Codex manages its own tool schemas over the Responses API; the proxy
        # passes them through rather than filtering against an allowlist.
        "tools_json": "[]",
        "approval_policy": "auto",
        "max_steps": 15,
        "is_active": "false",
    },
]


def upgrade() -> None:
    """Seed one profile per runtime.

    Idempotent: each insert is skipped when a profile of that name already
    exists (e.g. created via the management website or a manual dev seed), so it
    never collides on the unique name or the primary key.
    """
    for profile in _PROFILES:
        base_url = (
            "NULL" if profile["base_url"] is None else f"'{profile['base_url']}'"
        )
        op.execute(
            f"""
            INSERT INTO public.agent_profile
                (profile_id, name, model, framework_version, base_url, api_key_ref,
                 tools_json, approval_policy, max_steps, temperature,
                 max_context_tokens, is_active, created_at)
            SELECT '{profile["profile_id"]}', '{profile["name"]}',
                   '{profile["model"]}', '{profile["framework_version"]}',
                   {base_url}, '{profile["api_key_ref"]}',
                   '{profile["tools_json"]}', '{profile["approval_policy"]}',
                   {profile["max_steps"]}, NULL, NULL, {profile["is_active"]}, now()
            WHERE NOT EXISTS (
                SELECT 1 FROM public.agent_profile WHERE name = '{profile["name"]}'
            );
            """
        )


def downgrade() -> None:
    """Remove only the rows this migration could have inserted (by their fixed
    ids), so profiles created by other means are left untouched."""
    ids = ", ".join(f"'{profile['profile_id']}'" for profile in _PROFILES)
    op.execute(f"DELETE FROM public.agent_profile WHERE profile_id IN ({ids});")
