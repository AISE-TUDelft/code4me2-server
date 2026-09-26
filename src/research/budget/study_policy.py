"""Study-level budget policy helpers shared by the study and budget routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from database.research_schemas import StudyAgentProfile
from research.study.agents.enums import METERED_FRAMEWORKS

from .errors import PriceMissing
from .pricing import MICRO_USD_PER_USD, get_model_price

__all__ = [
    "budget_policy_payload",
    "metered_selections",
    "micro_to_usd_string",
    "missing_prices",
]


def micro_to_usd_string(micro_usd: Optional[int]) -> Optional[str]:
    if micro_usd is None:
        return None
    return f"{Decimal(int(micro_usd)) / MICRO_USD_PER_USD:.2f}"


def metered_selections(db: Any, study_id: uuid.UUID) -> list[dict[str, Any]]:
    """The study's frozen arms whose runtime spends from the shared key."""
    rows = db.execute(
        select(StudyAgentProfile)
        .where(StudyAgentProfile.study_id == study_id)
        .order_by(StudyAgentProfile.selection_order.asc())
    ).scalars().all()
    selections = []
    for row in rows:
        snapshot = row.profile_snapshot_json or {}
        framework = str(snapshot.get("framework_version") or "").strip().lower()
        if framework not in METERED_FRAMEWORKS:
            continue
        connection_id = snapshot.get("connection_id")
        selections.append(
            {
                "profile_id": str(row.profile_id),
                "name": snapshot.get("name", ""),
                "model": snapshot.get("model", ""),
                "framework_version": framework,
                "connection_id": str(connection_id) if connection_id else None,
            }
        )
    return selections


def missing_prices(db: Any, selections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Metered arms whose frozen model has no price row (they would fail closed)."""
    missing = []
    for selection in selections:
        connection_id = selection.get("connection_id")
        try:
            get_model_price(
                db,
                uuid.UUID(str(connection_id)) if connection_id else None,
                str(selection.get("model") or ""),
            )
        except (PriceMissing, ValueError):
            missing.append(
                {
                    "profile_id": selection.get("profile_id"),
                    "name": selection.get("name"),
                    "model": selection.get("model"),
                    "connection_id": connection_id,
                }
            )
    return missing


def budget_policy_payload(study: Any, selections: list[dict[str, Any]]) -> dict[str, Any]:
    updated_at = getattr(study, "inference_budget_updated_at", None)
    fraction = getattr(study, "inference_budget_warning_fraction", None)
    default = int(getattr(study, "inference_budget_default_micro_usd", 0) or 0)
    return {
        "metered": bool(selections),
        "metered_profile_ids": [selection["profile_id"] for selection in selections],
        "default_budget_micro_usd": default,
        "default_budget_usd": micro_to_usd_string(default),
        "warning_fraction": float(fraction) if fraction is not None else 0.8,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else None,
        "updated_by": getattr(study, "inference_budget_updated_by", None),
    }
