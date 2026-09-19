#!/usr/bin/env python3
"""Export the curated plugin-facing OpenAPI snapshot from the FastAPI routers.

The tracked ``openapi.json`` is **not** a full API dump: it is a curated
18-path snapshot of the plugin-facing routes. Before this script the generation
invocation was uncommitted, so the snapshot drifted from the source. This
exporter is that invocation, and it is deterministic:

1. build the route table without the application config/database (the schema
   only depends on the routers and their Pydantic models);
2. keep exactly the curated paths (``CURATED_PATHS``);
3. prune ``components`` to the schemas transitively referenced by those paths;
4. preserve the path order already on disk so a refresh produces a reviewable
   diff;
5. write the pretty server snapshot.

Usage::

    python scripts/dev/export_openapi.py                 # refresh openapi.json
    python scripts/dev/export_openapi.py --check         # CI: fail if stale

This exporter owns the **server** snapshot only. The IntelliJ plugin's bundled
client contract (`code4me2/src/main/resources/backend/api/openapi.json`) is a
different, larger artifact (22 paths, including session deactivation, user
lookup and password reset) that is the input for the generated Kotlin client.
Do not copy this snapshot over it: doing so silently drops client paths. The
Kotlin client in `code4me2/generated` is regenerated separately; this script
prints the exact ``openapi-generator-cli`` invocation to use.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

# The curated plugin-facing snapshot. Adding a path here is a deliberate
# snapshot change, never an automatic consequence of adding a server route.
# (This is not the plugin's bundled client contract; see the module docstring.)
CURATED_PATHS: tuple[str, ...] = (
    "/api/session/acquire",
    "/api/project/create",
    "/api/project/activate",
    "/api/user/create",
    "/api/user/update",
    "/api/user/delete",
    "/api/user/authenticate",
    "/api/user/verify/check",
    "/api/user/verify/resend",
    "/api/user/verify/",
    "/api/completion/request",
    "/api/completion/feedback",
    "/api/completion/{query_id}",
    "/api/completion/multi-file-context/update",
    "/api/chat/request",
    "/api/chat/get/{page_number}",
    "/api/chat/delete/{chat_id}",
    "/api/ping",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _build_schema() -> dict[str, Any]:
    """Build the OpenAPI schema without importing the configured application."""
    sys.path.insert(0, str(SRC_ROOT))
    from fastapi import FastAPI  # noqa: PLC0415 - import after sys.path is set

    from backend.routers import router  # noqa: PLC0415

    app = FastAPI(
        title="Code4Me V2 API",
        description="The complete API for Code4Me V2",
        version="1.0.0",
    )
    app.include_router(router, prefix="/api")
    return app.openapi()


def _refs(node: Any) -> Iterator[str]:
    """Yield every ``#/components/...`` reference under ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/"):
                yield value
            else:
                yield from _refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _refs(item)


def _prune_components(spec: dict[str, Any]) -> None:
    """Keep only the components transitively referenced by the kept paths."""
    components = spec.get("components", {})
    used: dict[str, set[str]] = {}

    def record(ref: str) -> bool:
        parts = ref.split("/")
        if len(parts) < 4:
            return False
        section, name = parts[2], "/".join(parts[3:])
        names = used.setdefault(section, set())
        if name in names:
            return False
        names.add(name)
        return True

    for ref in _refs(spec.get("paths", {})):
        record(ref)

    changed = True
    while changed:
        changed = False
        for section, names in list(used.items()):
            for name in list(names):
                for ref in _refs(components.get(section, {}).get(name)):
                    changed = record(ref) or changed

    pruned: dict[str, Any] = {}
    for section, entries in components.items():
        kept = {name: node for name, node in entries.items() if name in used.get(section, set())}
        if kept:
            pruned[section] = kept
    spec["components"] = pruned


def _existing_path_order(output: Path) -> list[str]:
    if not output.is_file():
        return []
    try:
        current = json.loads(output.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    return list(current.get("paths", {}))


def curated_spec(output: Path) -> dict[str, Any]:
    """The deterministic curated contract for ``output`` (used for ordering)."""
    spec = _build_schema()
    paths = spec.get("paths", {})
    missing = [path for path in CURATED_PATHS if path not in paths]
    if missing:
        raise SystemExit(
            "the server no longer exposes curated client path(s): " + ", ".join(missing)
        )
    # Preserve the on-disk order so a refresh diff only shows real changes.
    order = [path for path in _existing_path_order(output) if path in CURATED_PATHS]
    order += [path for path in CURATED_PATHS if path not in order]
    spec["paths"] = {path: paths[path] for path in order}
    _prune_components(spec)
    return spec


def _render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "openapi.json",
        help="server snapshot to write (default: <repo>/openapi.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the tracked snapshot is not identical to the export",
    )
    args = parser.parse_args()

    spec = curated_spec(args.output)
    rendered = _render(spec)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != rendered:
            print(f"{args.output} is stale; run scripts/dev/export_openapi.py", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date ({len(spec['paths'])} paths)")
        return 0

    args.output.write_text(rendered, encoding="utf-8")
    print(f"wrote {args.output} ({len(spec['paths'])} paths, {len(spec['components'].get('schemas', {}))} schemas)")

    print(
        "Kotlin client (code4me2/generated) is regenerated separately:\n"
        "  cd code4me2 && npx --yes @openapitools/openapi-generator-cli@7.13.0 generate \\\n"
        "    -i src/main/resources/backend/api/openapi.json -g kotlin \\\n"
        "    -o generated --additional-properties=library=jvm-okhttp4,serializationLibrary=moshi"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
