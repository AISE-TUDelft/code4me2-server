from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from code4me2_agent.acp_runtime import run_acp_stdio
from code4me2_agent.config import AgentConfig


DEFAULT_CONFIG_PATH = os.path.expanduser("~/.code4me/agent-config.json")
DEFAULT_ACP_JSON_PATH = os.path.expanduser("~/.code4me/acp.json")

# Scaffold written by `code4me2-agent init`. Deliberately specifies no model,
# provider or API key: those come from the agent profile the backend assigns
# (GET /api/acp/agent-config), so hardcoding them here would let a stale local
# file silently override a study assignment.
#
# `max_iterations` and the command allowlist are also server-overridable — the
# values here only apply before the first successful authentication.
DEFAULT_AGENT_CONFIG: dict[str, object] = {
    "adapter": {
        "name": "openai_compatible_react",
        "max_iterations": 10,
        "provider": {
            # Route model calls through the backend by default. The assigned
            # profile switches this to a direct provider when it names a
            # base_url of its own.
            "kind": "code4me_backend",
            # base_url / model / api_key_env intentionally omitted — supplied
            # by the backend's agent-config after authentication.
        },
        "memory_window": {
            "scope": "session",
            "strategy": "token_window",
            "max_messages": 20,
            "max_tokens": 6000,
        },
    },
    "commands": {
        # Starting allowlist for local command execution, narrowed further by
        # the server's agent-config when one is configured. Write-capable
        # entries are included because the agent's purpose is editing code; the
        # workspace confinement in file_tools is what bounds the damage.
        "allowlist": [
            "pwd", "ls", "grep", "cat", "git", "rg", "echo",
            "bash", "sh", "mkdir", "rm", "mv", "cp", "chmod",
            "which", "head", "tail", "sort", "wc", "diff", "find", "make",
        ],
    },
    "telemetry": {
        "trace_path": ".code4me/acp-trace.jsonl",
        # Local JSONL trace detail. Note this does NOT control what the server
        # stores: content persistence is decided server-side from the user's
        # store_agent_content preference, regardless of this flag.
        "raw_capture_enabled": True,
    },
}

DEFAULT_ACP_JSON: dict[str, object] = {
    "default_mcp_settings": {
        "use_idea_mcp": True,
        "use_custom_mcp": True,
    },
    "agent_servers": {
        "Code4Me Agent": {
            "command": "code4me2-agent",
            "args": [],
        },
    },
}


def _resolve_agent_command() -> str:
    current_command = Path(sys.argv[0]).name
    if current_command:
        resolved_command = shutil.which(current_command)
        if resolved_command:
            return resolved_command

    for command in ("code4me2-agent"):
        resolved_command = shutil.which(command)
        if resolved_command:
            return resolved_command

    return sys.argv[0] or "code4me2-agent"


def _build_acp_json() -> dict[str, object]:
    acp_json = json.loads(json.dumps(DEFAULT_ACP_JSON))
    acp_json["agent_servers"]["Code4Me Agent"]["command"] = _resolve_agent_command()
    return acp_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Code4Me ACP agent over stdio.")
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        help="Path to agent config JSON file (default: ~/.code4me/agent-config.json).",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Install default config files to ~/.code4me/ and print setup instructions.",
    )
    return parser


def configure_logging() -> None:
    level_name = os.environ.get("CODE4ME_AGENT_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _do_setup() -> None:
    config_dir = Path("~/.code4me").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)

    agent_config_path = config_dir / "agent-config.json"
    if not agent_config_path.exists():
        agent_config_path.write_text(json.dumps(DEFAULT_AGENT_CONFIG, indent=2) + "\n")
        print(f"Created {agent_config_path}")
    else:
        print(f"Already exists: {agent_config_path} (skipped)")

    acp_json_path = DEFAULT_ACP_JSON_PATH
    if not os.path.exists(acp_json_path):
        with open(acp_json_path, "w") as f:
            json.dump(_build_acp_json(), f, indent=2)
            f.write("\n")
        print(f"Created {acp_json_path}")
    else:
        print(f"Already exists: {acp_json_path} (skipped)")

    print()
    print("Setup complete. To use the Code4Me agent in JetBrains AI Assistant:")
    print("  1. Make sure JetBrains AI Assistant points to the acp.json above")
    print("     (Settings → Tools → AI Assistant → Custom Agent Server)")
    print("  2. Restart IntelliJ")
    print("  3. Run: docker compose -f docker-compose.dev-arm.yaml up")
    print("  4. Run: ollama serve")
    print("  5. Run: ./gradlew runIde")


def main(argv: list[str] | None = None) -> None:
    configure_logging()
    args = build_parser().parse_args(argv)

    if args.setup:
        _do_setup()
        sys.exit(0)

    config = AgentConfig.from_file(args.config)
    asyncio.run(run_acp_stdio(config))


if __name__ == "__main__":
    main()
