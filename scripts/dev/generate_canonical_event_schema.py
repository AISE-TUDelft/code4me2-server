#!/usr/bin/env python3
"""Regenerate the canonical telemetry JSON Schema from the Python model.

``CanonicalEventV1`` (``research.telemetry.models``) is the single source of
truth for the event envelope; the closed vocabularies come from
``research.telemetry.enums``. This script writes the language-neutral contract
consumed by generated clients.

It is intentionally dependency-free and side-effect limited to the schema file.
The test suite asserts that the checked-in file equals the generated output, so a
model change that is not accompanied by a regeneration fails fast.

Usage (from ``code4me2-server/``)::

    python scripts/dev/generate_canonical_event_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from research.telemetry.schema import (  # noqa: E402
    CANONICAL_EVENT_V1_SCHEMA_PATH,
    write_canonical_event_v1_schema,
)


def main() -> int:
    path = write_canonical_event_v1_schema(CANONICAL_EVENT_V1_SCHEMA_PATH)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
