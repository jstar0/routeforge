"""Multi-seed stability artifacts for RouteForge."""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable
from math import sqrt
from pathlib import Path
from statistics import fmean
from typing import Any

from .benchmark import benchmark_payload


def parse_seed_list(value: str) -> tuple[int, ...]:
    pieces = [piece.strip() for piece in value.split(",")]
    if not pieces or any(not piece for piece in pieces):
        raise ValueError("seeds must be a comma-separated list of integers")
    try:
        seeds = tuple(int(piece) for piece in pieces)
    except ValueError as exc:
        raise ValueError("seeds must be a comma-separated list of integers") from exc
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")
    return seeds


def seed_sweep_payload(seeds: Iterable[int]) -> dict[str, Any]:
    seed_tuple = tuple(seeds)
    if not seed_tuple:
        raise ValueError("at least one seed is required")
    if len(set(seed_tuple)) != len(seed_tuple) or any(
        isinstance(seed, bool) or not isinstance(seed, int) for seed in seed_tuple
    ):
        raise ValueError("seeds must be unique integers")
    runs: list[dict[str, Any]] = []
    for seed in seed_tuple:
        payload = benchmark_payload(seed)
        compact = payload["compact"]
        stress = payload["stress"]
        runs.append(
            {
                "seed": seed,
                "compactAcoCost": compact["acoCost"],
                "compactGreedyCost": compact["greedyCost"],
                "compactImprovementPct": compact["improvementPct"],
                "compactFeasible": compact["feasible"],
                "stressAcoCost": stress["acoCost"],
                "stressGreedyCost": stress["greedyCost"],
                "stressImprovementPct": stress["improvementPct"],
                "stressFeasible": stress["feasible"],
            }
        )
    numeric_fields = (
        "compactAcoCost",
        "compactImprovementPct",
        "stressAcoCost",
        "stressImprovementPct",
    )
    aggregate: dict[str, dict[str, float]] = {}
    for field in numeric_fields:
        values = [float(run[field]) for run in runs]
        mean = fmean(values)
        aggregate[field] = {
            "mean": round(mean, 8),
            "pstdev": round(sqrt(fmean((value - mean) ** 2 for value in values)), 8),
        }
    aggregate["compactFeasibleRate"] = {
        "mean": round(sum(bool(run["compactFeasible"]) for run in runs) / len(runs), 8),
        "pstdev": 0.0,
    }
    aggregate["stressFeasibleRate"] = {
        "mean": round(sum(bool(run["stressFeasible"]) for run in runs) / len(runs), 8),
        "pstdev": 0.0,
    }
    return {"sweepVersion": 1, "seeds": list(seed_tuple), "runs": runs, "aggregate": aggregate}


def write_sweep(output_dir: str | Path, seeds: Iterable[int]) -> dict[str, Any]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = seed_sweep_payload(seeds)
    write_text(directory / "sweep.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_csv(directory / "sweep.csv", payload["runs"])
    write_text(directory / "report.md", markdown_report(payload))
    write_manifest(directory)
    return payload


def write_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    fields = tuple(runs[0]) if runs else ("seed",)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(runs)


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# RouteForge seed sweep",
        "",
        "The synthetic routing benchmark repeated over explicit independent seeds.",
        "",
        f"- seeds: `{', '.join(str(seed) for seed in payload['seeds'])}`",
        "",
        "| seed | compact improvement | compact feasible | stress improvement | stress feasible |",
        "|---:|---:|:---:|---:|:---:|",
    ]
    for run in payload["runs"]:
        lines.append(
            f"| {run['seed']} | {run['compactImprovementPct']:.4f}% | "
            f"{'yes' if run['compactFeasible'] else 'no'} | {run['stressImprovementPct']:.4f}% | "
            f"{'yes' if run['stressFeasible'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Aggregate",
            "",
            *[
                f"- `{field}` — mean `{summary['mean']:.8f}`, population σ `{summary['pstdev']:.8f}`"
                for field, summary in payload["aggregate"].items()
            ],
            "",
            "Variation is a stability descriptor over invented routing instances, not a confidence interval or "
            "a claim about real fleet performance.",
            "",
        ]
    )
    return "\n".join(lines)


def write_manifest(directory: Path) -> None:
    manifest = {
        "schemaVersion": 1,
        "files": [
            {"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path.read_bytes())}
            for path in sorted(directory.glob("*"))
            if path.is_file() and path.name != "manifest.json"
        ],
    }
    write_text(directory / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
