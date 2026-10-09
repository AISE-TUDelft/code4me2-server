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
SUBSETS_DIR = Path(__file__).resolve().parents[1] / "subsets"


def load_rows(dataset: str = DATASET, split: str = "test") -> list[dict]:
    from datasets import load_dataset

    return [normalize_row(dict(row)) for row in load_dataset(dataset, split=split)]


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
