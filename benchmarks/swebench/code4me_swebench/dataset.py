"""Task selection from SWE-bench Verified.

``SWE-bench/SWE-bench_Verified`` is used (not ``princeton-nlp/...``) because it
carries the per-instance ``image`` and ``eval_script`` that swebench 5 grades with.
"""

from __future__ import annotations

import random
from collections import defaultdict
from pathlib import Path

DATASET = "SWE-bench/SWE-bench_Verified"
REBENCH_DATASET = "nebius/SWE-rebench-leaderboard"
# The dataset revision whose split 2026_03 is exactly the leaderboard window
# 2026-03-01 -> 2026-05-15 (110 tasks; checked 2026-10-14). Pinned so a later
# fix to the dataset cannot silently change the task set between runs.
REBENCH_REVISION = "34d5a58864acf91613740a09ec5d205228dcfa39"
SUBSETS_DIR = Path(__file__).resolve().parents[1] / "subsets"


def default_revision(dataset: str) -> str | None:
    return REBENCH_REVISION if dataset == REBENCH_DATASET else None


def load_rows(dataset: str = DATASET, split: str = "test", revision: str | None = None) -> list[dict]:
    from datasets import load_dataset

    rows = load_dataset(dataset, split=split, revision=revision or default_revision(dataset))
    return [normalize_row(dict(row)) for row in rows]


def in_window(rows: list[dict], start: str, end: str) -> list[str]:
    """Ids created in [start, end) (YYYY-MM-DD), oldest first: a leaderboard time
    window. SWE-rebench windows are half-month aligned and end-exclusive."""
    chosen = [row for row in rows if start <= str(row.get("created_at"))[:10] < end]
    return [row["instance_id"] for row in sorted(chosen, key=lambda r: (str(r.get("created_at")), r["instance_id"]))]


def normalize_row(row: dict) -> dict:
    """Give every row an ``image`` (SWE-rebench names it image_name/docker_image)."""
    if not row.get("image"):
        image = row.get("image_name") or row.get("docker_image")
        if image:
            row["image"] = image
    return row


def default_grader(dataset_name: str) -> str:
    return "rebench" if "rebench" in dataset_name.lower() else "swebench"


def read_subset(path: Path) -> list[str]:
    ids = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path} lists an instance twice")
    return ids


def resolve_subset(name_or_path: str) -> Path:
    path = Path(name_or_path)
    if path.exists():
        return path
    bundled = SUBSETS_DIR / f"{name_or_path}.txt"
    if bundled.exists():
        return bundled
    raise FileNotFoundError(f"no subset file {name_or_path!r} (bundled: {sorted(p.stem for p in SUBSETS_DIR.glob('*.txt'))})")


def select_rows(rows: list[dict], ids: list[str]) -> list[dict]:
    by_id = {row["instance_id"]: row for row in rows}
    missing = [iid for iid in ids if iid not in by_id]
    if missing:
        raise KeyError(f"instances not in the dataset: {missing}")
    return [by_id[iid] for iid in ids]


def stratified_sample(rows: list[dict], count: int, *, seed: int) -> list[str]:
    """Pick ``count`` ids proportionally over (repo, difficulty) cells.

    Allocation uses the largest-remainder method, so every cell gets its
    proportional share rounded fairly; ties break by cell name for determinism.
    """
    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        cells[(row["repo"], row.get("difficulty") or "unknown")].append(row["instance_id"])
    total = len(rows)
    quotas = {cell: len(ids) * count / total for cell, ids in cells.items()}
    allocation = {cell: int(quota) for cell, quota in quotas.items()}
    remaining = count - sum(allocation.values())
    by_remainder = sorted(quotas, key=lambda cell: (-(quotas[cell] - allocation[cell]), cell))
    for cell in by_remainder[:remaining]:
        allocation[cell] += 1
    rng = random.Random(seed)
    chosen: list[str] = []
    for cell in sorted(cells):
        chosen.extend(rng.sample(sorted(cells[cell]), allocation[cell]))
    return sorted(chosen)


def newest(rows: list[dict], count: int) -> list[str]:
    """The ``count`` most recently created tasks (least likely to be in training data)."""
    ordered = sorted(rows, key=lambda row: (str(row.get("created_at") or ""), row["instance_id"]))
    return sorted(row["instance_id"] for row in ordered[-count:])
