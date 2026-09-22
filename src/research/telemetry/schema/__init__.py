"""Language-neutral JSON Schema for the canonical telemetry envelope.

``canonical_event_v1.json`` is **generated** from
:class:`research.telemetry.models.CanonicalEventV1`, which is the single source
of truth for the event schema; the closed vocabularies (event types, coverage
states, fidelity) come from :mod:`research.telemetry.enums`. It is the
cross-language contract consumed by generated clients.

The generation is deterministic and is asserted by the test suite, so the
checked-in file can never silently drift from the Python model::

    python scripts/dev/generate_canonical_event_schema.py

Unknown ``source``/``lifecycle`` semantics are identical to the model: both
accept an unknown token verbatim (the original is preserved on
``unknown_source``/``unknown_lifecycle_state``), so the schema does not enum-
restrict them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research.telemetry.enums import CANONICAL_SCHEMA_VERSION, CanonicalEventType
from research.telemetry.models import CanonicalEventV1

SCHEMA_DIR = Path(__file__).resolve().parent
CANONICAL_EVENT_V1_SCHEMA_PATH = SCHEMA_DIR / "canonical_event_v1.json"

_SCHEMA_ID = "https://code4me.invalid/research/schema/canonical_event_v1.json"
_SCHEMA_DESCRIPTION = (
    "Language-neutral canonical telemetry envelope (schema version "
    f"{CANONICAL_SCHEMA_VERSION}). Canonical event types are vendor-independent "
    "concepts; SECRET fields never appear because the privacy filter removes "
    "them before serialization."
)


def build_canonical_event_v1_schema() -> dict[str, Any]:
    """Build the canonical event JSON Schema from the Python model."""
    schema = CanonicalEventV1.model_json_schema(ref_template="#/$defs/{model}")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = _SCHEMA_ID
    schema["title"] = "CanonicalEventV1"
    schema["description"] = _SCHEMA_DESCRIPTION

    properties = schema.setdefault("properties", {})
    # The envelope version is required and pinned; it is never defaulted.
    properties["schema_version"] = {"const": CANONICAL_SCHEMA_VERSION}
    # Per-emitter sequence is a strictly positive monotonic counter.
    properties["emitter_sequence"] = {
        **properties["emitter_sequence"],
        "minimum": 1,
    }
    # The builder guarantees a known canonical type (an unrecognized source
    # construct becomes ``unknown_source_event``), so the wire vocabulary is
    # closed even though the raw model field is a plain string.
    properties["event_type"] = {
        "type": "string",
        "enum": [event_type.value for event_type in CanonicalEventType],
    }
    return schema


def serialize_canonical_event_v1_schema(schema: dict[str, Any]) -> str:
    """Serialize the schema deterministically (stable for review and diffs)."""
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def write_canonical_event_v1_schema(
    path: Path = CANONICAL_EVENT_V1_SCHEMA_PATH,
) -> Path:
    """Regenerate the checked-in JSON Schema from the Python model."""
    path.write_text(
        serialize_canonical_event_v1_schema(build_canonical_event_v1_schema()),
        encoding="utf-8",
    )
    return path


def load_canonical_event_v1_schema() -> dict[str, Any]:
    """Load the canonical event JSON Schema."""
    return json.loads(CANONICAL_EVENT_V1_SCHEMA_PATH.read_text())


__all__ = [
    "CANONICAL_EVENT_V1_SCHEMA_PATH",
    "SCHEMA_DIR",
    "build_canonical_event_v1_schema",
    "load_canonical_event_v1_schema",
    "serialize_canonical_event_v1_schema",
    "write_canonical_event_v1_schema",
]
