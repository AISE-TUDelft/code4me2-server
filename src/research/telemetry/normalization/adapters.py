"""Static, server-side allowlist for optional telemetry enrichment adapters.

Adapter seam recorded from the Task 09 decisions (agent runtime extensibility
and Codex):

1. Codex speaks ACP through the generic path. Its observations are normalized
   by :class:`~research.telemetry.normalization.generic_acp.GenericAcpNormalizer`;
   no second, vendor-specific telemetry schema is introduced.
2. Distribution for the Codex integration is ``BYOA_EXTERNAL`` only. The
   ``PACKAGED`` branch remains for the managed ``code4me2-agent`` release.
3. Telemetry stays generic-only, with this *allowlisted* adapter seam as the
   single optional enrichment point. There is no Codex-specific enrichment
   enabled now; an adapter can only add fields, never rewrite or delete the
   generic canonical event (see
   :func:`~research.telemetry.normalization.generic_acp.enrich_with_adapter`).
4. Adapters are resolved from a **static server-side allowlist keyed by**
   ``AdapterRef.adapter_id``. An unknown or absent id falls back to the generic
   result. A caller-provided module path, dotted class name or factory is never
   imported, evaluated or otherwise executed; only an id already registered in
   this process can ever produce an adapter.

The registry is empty by default, so ``resolve_adapter`` returns ``None`` and
``normalize_acp_observation`` returns the untouched generic result. A test or a
trusted deployment registration helper populates it explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .generic_acp import AgentAdapter, GenericAcpNormalizer, enrich_with_adapter
from .models import NormalizationResultV1
from research.telemetry.privacy import PrivacyPolicy, filter_payload

__all__ = [
    "AdapterSpec",
    "clear_adapters_for_tests",
    "normalize_acp_observation",
    "register_adapter",
    "registered_adapter_ids",
    "registered_adapters",
    "resolve_adapter",
    "unregister_adapter",
]

#: An adapter factory is invoked with no arguments. It is registered
#: server-side; no client input can ever reach it.
AdapterFactory = Callable[[], AgentAdapter]

_VERSION_TOKEN = re.compile(r"\d+")
_RANGE_CLAUSE = re.compile(r"^\s*(>=|<=|==|>|<|=)?\s*([0-9][0-9A-Za-z.\-+]*)\s*$")


@dataclass(frozen=True)
class AdapterSpec:
    """Allowlist metadata for one registered adapter.

    ``supported_release_ranges`` is an immutable tuple of range expressions
    (for example ``">=1.2.0,<1.3.0"``) the adapter claims to drive. A release
    version outside every declared range resolves to no adapter, i.e. the
    generic fallback.
    """

    adapter_id: str
    adapter_version: str
    supported_release_ranges: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.adapter_id.strip():
            raise ValueError("adapter_id must not be blank")
        if not self.adapter_version.strip():
            raise ValueError("adapter_version must not be blank")
        for expression in self.supported_release_ranges:
            if not expression.strip():
                raise ValueError("supported_release_ranges entries must not be blank")


@dataclass(frozen=True)
class _Registration:
    spec: AdapterSpec
    factory: AdapterFactory


#: The process-wide allowlist. It is intentionally private: callers may only
#: add/remove entries through the helpers below, and never through a name that
#: came from request input.
_REGISTRY: dict[str, _Registration] = {}


def register_adapter(
    spec: AdapterSpec,
    factory: AdapterFactory,
    *,
    replace: bool = False,
) -> None:
    """Add one allowlisted adapter.

    Raises :class:`ValueError` when the id is already registered and
    ``replace`` is false, so an accidental duplicate can never silently
    shadow an approved adapter.
    """
    if not callable(factory):
        raise TypeError("factory must be callable")
    if spec.adapter_id in _REGISTRY and not replace:
        raise ValueError(f"adapter {spec.adapter_id!r} is already registered")
    _REGISTRY[spec.adapter_id] = _Registration(spec=spec, factory=factory)


def unregister_adapter(adapter_id: str) -> bool:
    """Remove one allowlisted adapter; returns whether it existed."""
    return _REGISTRY.pop((adapter_id or "").strip(), None) is not None


def registered_adapter_ids() -> tuple[str, ...]:
    """The allowlisted adapter ids, in a stable order."""
    return tuple(sorted(_REGISTRY))


def registered_adapters() -> tuple[AdapterSpec, ...]:
    """The allowlisted adapter specs, in a stable order."""
    return tuple(_REGISTRY[adapter_id].spec for adapter_id in registered_adapter_ids())


def clear_adapters_for_tests() -> None:
    """Drop every registration.

    Test-only isolation helper: the production allowlist is static, so no
    runtime path should call this.
    """
    _REGISTRY.clear()


def _adapter_id_of(adapter_ref_or_id: Any) -> Optional[str]:
    """Extract a plain adapter id from a ref/string without trusting anything else."""
    if adapter_ref_or_id is None:
        return None
    if isinstance(adapter_ref_or_id, str):
        candidate = adapter_ref_or_id.strip()
        return candidate or None
    attribute = getattr(adapter_ref_or_id, "adapter_id", None)
    if isinstance(attribute, str):
        candidate = attribute.strip()
        return candidate or None
    return None


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in _VERSION_TOKEN.findall(str(value)))


def _clause_includes(clause: str, version: str) -> bool:
    match = _RANGE_CLAUSE.match(clause)
    if match is None:
        # An unparseable clause must not silently widen the allowlist.
        return False
    operator = match.group(1) or "=="
    bound = match.group(2)
    current = _version_tuple(version)
    if not current:
        return False
    target = _version_tuple(bound)
    if operator == ">=":
        return current >= target
    if operator == ">":
        return current > target
    if operator == "<=":
        return current <= target
    if operator == "<":
        return current < target
    return current == target


def _range_includes(expression: str, version: str) -> bool:
    """Whether a comma-separated range expression includes ``version``."""
    clauses = [clause.strip() for clause in str(expression).split(",") if clause.strip()]
    if not clauses:
        return False
    return all(_clause_includes(clause, version) for clause in clauses)


def resolve_adapter(
    adapter_ref_or_id: Any,
    *,
    release_version: Optional[str] = None,
) -> Optional[AgentAdapter]:
    """Resolve an allowlisted adapter, or ``None`` for the generic fallback.

    ``None`` is returned when no ref is supplied, when the id is unknown, or
    when the release version cannot be proven compatible. Only an id that is
    already present in the static allowlist can produce an adapter; the
    ``adapter_ref_or_id`` value is used for its id alone and is never imported
    or evaluated.

    Version activation is fail-closed and uses the **registered** spec's ranges
    only: a caller-supplied ref can never widen (or narrow) the allowlisted
    window, and a registration that declares no usable range cannot be
    activated for a known version.
    """
    adapter_id = _adapter_id_of(adapter_ref_or_id)
    if adapter_id is None:
        return None
    registration = _REGISTRY.get(adapter_id)
    if registration is None:
        return None
    if release_version is not None:
        ranges = [
            str(item)
            for item in registration.spec.supported_release_ranges
            if str(item).strip()
        ]
        if not ranges:
            # No declared compatibility window: refuse rather than activate an
            # unverifiable adapter version (the caller falls back to generic).
            return None
        if not any(_range_includes(expression, release_version) for expression in ranges):
            return None
    return registration.factory()


def normalize_acp_observation(
    observation: Any,
    *,
    adapter_ref: Any = None,
    release_version: Optional[str] = None,
    normalizer: Optional[GenericAcpNormalizer] = None,
    source_event_id: Optional[str] = None,
    direction: Optional[str] = None,
    privacy_policy: Optional[PrivacyPolicy] = None,
) -> NormalizationResultV1:
    """Normalize one raw ACP observation, then optionally enrich it.

    The generic ACP result is authoritative: it is returned **unchanged** when
    no adapter resolves. When an adapter does resolve, every candidate is run
    through
    :func:`~research.telemetry.normalization.generic_acp.enrich_with_adapter`,
    so the generic payload still wins on conflict and the adapter version is
    recorded on the enriched candidate and the result.

    Adapter-injected fields are not trusted: after enrichment every candidate
    payload is re-run through the privacy filter (deny-by-default when no
    ``privacy_policy`` is supplied), so a vendor adapter cannot widen what
    leaves the normalizer. The generic path is untouched.

    A normalizer instance holds bounded per-stream correlation state, so callers
    observing a stream must pass the same ``normalizer`` across calls.
    """
    engine = normalizer if normalizer is not None else GenericAcpNormalizer()
    result = engine.normalize(
        observation,
        source_event_id=source_event_id,
        direction=direction,
    )
    adapter = resolve_adapter(adapter_ref, release_version=release_version)
    if adapter is None:
        return result
    enriched = [enrich_with_adapter(candidate, adapter) for candidate in result.candidates]
    policy = privacy_policy if privacy_policy is not None else PrivacyPolicy.default()
    filtered = []
    for candidate in enriched:
        payload, _summary = filter_payload(candidate.payload, policy)
        filtered.append(candidate.model_copy(update={"payload": payload}))
    return result.model_copy(
        update={
            "candidates": filtered,
            "adapter_version": getattr(adapter, "adapter_version", None),
        }
    )
