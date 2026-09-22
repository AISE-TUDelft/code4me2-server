"""TSCH-04: the checked-in canonical JSON Schema never drifts from the model.

``CanonicalEventV1`` is the single source of truth; the committed
``canonical_event_v1.json`` is generated from it. These tests fail fast when a
model change is not accompanied by ``scripts/dev/generate_canonical_event_schema.py``.
"""

from __future__ import annotations

import json

from research.telemetry.schema import (
    CANONICAL_EVENT_V1_SCHEMA_PATH,
    build_canonical_event_v1_schema,
    load_canonical_event_v1_schema,
    serialize_canonical_event_v1_schema,
)


def test_committed_schema_equals_the_serialized_model():
    generated = serialize_canonical_event_v1_schema(build_canonical_event_v1_schema())
    assert load_canonical_event_v1_schema() == json.loads(generated)


def test_committed_schema_file_is_byte_identical_to_the_generated_output():
    generated = serialize_canonical_event_v1_schema(build_canonical_event_v1_schema())
    assert CANONICAL_EVENT_V1_SCHEMA_PATH.read_text() == generated


def test_emitter_sequence_requires_a_positive_integer():
    schema = load_canonical_event_v1_schema()
    assert schema["properties"]["emitter_sequence"]["minimum"] == 1
