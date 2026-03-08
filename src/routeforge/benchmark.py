"""Deterministic benchmark and artifact writers for RouteForge."""

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from statistics import fmean
from typing import Any

from .instance import generate_instance
from .solver import SolverConfig, solve

DEFAULT_CONFIG = SolverConfig(ants=12, iterations=24, local_search_passes=2)
STRESS_CONFIG = SolverConfig(ants=18, iterations=35, local_search_passes=2)

# Seeds used by the pheromone ablation. Fixed so the result is reproducible and
# wide enough that a single lucky instance cannot carry the conclusion.
ABLATION_SEEDS = (17, 29, 41, 53, 67, 71, 83, 97)


def _run(seed: int, customers: int, chargers: int, vehicles: int, config: SolverConfig) -> dict[str, Any]:
    instance = generate_instance(
        customers=customers,
        chargers=chargers,
        vehicles=vehicles,
        scale_km=40.0,
        seed=seed,
    )
    result = solve(instance, seed=seed, config=config)
    evaluation = result.evaluation
    baseline = result.baseline_evaluation
    improvement = (
        (baseline.total_cost - evaluation.total_cost) / baseline.total_cost * 100.0
        if baseline.total_cost > 1e-9
        else 0.0
    )
    return {
        "seed": seed,
        "customers": customers,
        "chargers": chargers,
        "vehicles": vehicles,
        "ants": config.ants,
        "iterations": config.iterations,
        "acoCost": round(evaluation.total_cost, 8),
        "greedyCost": round(baseline.total_cost, 8),
        "improvementPct": round(improvement, 8),
        "feasible": evaluation.feasible,
        "servedCustomers": len(evaluation.served_customers),
        "missingCustomers": len(evaluation.missing_customers),
        "violations": len(evaluation.violations),
        "distanceKm": round(evaluation.distance_km, 8),
        "chargingKwh": round(evaluation.charging_kwh, 8),
        "latenessMinutes": round(evaluation.lateness_minutes, 8),
        "convergenceStart": round(result.history[0], 8),
        "convergenceEnd": round(result.history[-1], 8),
    }


def _ablation_case(
    customers: int,
    chargers: int,
    vehicles: int,
    config: SolverConfig,
    seeds: tuple[int, ...] = ABLATION_SEEDS,
) -> dict[str, Any]:
    """Compare the battery-state pheromone tensor against a flat (from, to) matrix.

    Everything else is held identical, including the per-ant random stream, so
    the only difference is whether pheromone is indexed by battery bucket. The
    result is reported as measured, including when it does not favour the
    state-aware variant.
    """

    flat_config = replace(config, state_aware=False)
    rows: list[dict[str, Any]] = []
    for seed in seeds:
        instance = generate_instance(
            customers=customers, chargers=chargers, vehicles=vehicles, scale_km=40.0, seed=seed
        )
        aware = round(solve(instance, seed=seed, config=config).evaluation.total_cost, 8)
        flat = round(solve(instance, seed=seed, config=flat_config).evaluation.total_cost, 8)
        rows.append(
            {
                "seed": seed,
                "customers": customers,
                "stateAwareCost": aware,
                "flatPheromoneCost": flat,
                # Derived from the reported values, so the artifact adds up.
                # Positive means the state-aware tensor produced the cheaper plan.
                "costDelta": round(flat - aware, 8),
            }
        )
    deltas = [row["costDelta"] for row in rows]
    wins = sum(1 for delta in deltas if delta > 1e-9)
    losses = sum(1 for delta in deltas if delta < -1e-9)
    return {
        "customers": customers,
        "seeds": list(seeds),
        "runs": rows,
        "stateAwareWins": wins,
        "flatWins": losses,
        "ties": len(deltas) - wins - losses,
        "meanCostDelta": round(fmean(deltas), 8),
    }


def ablation_payload(seeds: tuple[int, ...] = ABLATION_SEEDS) -> dict[str, Any]:
    """Measure whether the battery-state pheromone dimension earns its place.

    Kept out of ``benchmark_payload`` on purpose: it runs four solves per seed,
    so folding it into the headline benchmark would make the fast path slow.
    """

    compact = _ablation_case(12, 3, 3, DEFAULT_CONFIG, seeds)
    stress = _ablation_case(30, 5, 5, STRESS_CONFIG, seeds)
    return {
        "ablationVersion": 1,
        "seeds": list(seeds),
        "question": "does indexing pheromone by battery state beat a flat (from, to) matrix?",
        "heldConstant": ["instance", "costModel", "fleet", "ants", "iterations", "randomStream"],
        "compact": compact,
        "stress": stress,
        "verdict": _ablation_verdict(compact, stress),
    }


def _ablation_verdict(compact: dict[str, Any], stress: dict[str, Any]) -> str:
    total = len(compact["seeds"]) + len(stress["seeds"])
    wins = compact["stateAwareWins"] + stress["stateAwareWins"]
    if wins * 2 > total + 1:
        return (
            f"state-aware cheaper on {wins}/{total} seeded instances; treat as a positive signal "
            "on these synthetic instances only"
        )
    return (
        f"not supported: state-aware cheaper on only {wins}/{total} seeded instances, so the "
        "battery-state dimension is not a demonstrated improvement at these scales"
    )


def benchmark_payload(seed: int = 17) -> dict[str, Any]:
    """Run one compact comparison plus one larger feasibility stress case."""

    compact = _run(seed, customers=12, chargers=3, vehicles=3, config=DEFAULT_CONFIG)
    stress = _run(seed, customers=30, chargers=5, vehicles=5, config=STRESS_CONFIG)
    return {
        "benchmarkVersion": 1,
        "seed": seed,
        "method": "state-aware-aco",
        "baseline": "deterministic-greedy",
        "compact": compact,
        "stress": stress,
        "boundary": {
            "synthetic": True,
            "comparisonHeldConstant": ["instance", "costModel", "seed", "fleet", "iterationBudget"],
            "claim": "instance-specific engineering comparison, not universal ACO superiority",
        },
    }


def write_benchmark(output_dir: str | Path, seed: int = 17) -> dict[str, Any]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = benchmark_payload(seed)
    write_text(directory / "benchmark.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_rows_csv(directory / "runs.csv", [payload["compact"], payload["stress"]])
    write_text(directory / "report.md", markdown_report(payload))
    write_manifest(directory)
    return payload


def write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "seed",
        "customers",
        "chargers",
        "vehicles",
        "ants",
        "iterations",
        "acoCost",
        "greedyCost",
        "improvementPct",
        "feasible",
        "servedCustomers",
        "missingCustomers",
        "violations",
        "distanceKm",
        "chargingKwh",
        "latenessMinutes",
        "convergenceStart",
        "convergenceEnd",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_ablation(output_dir: str | Path, seeds: tuple[int, ...] = ABLATION_SEEDS) -> dict[str, Any]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    payload = ablation_payload(seeds)
    write_text(directory / "ablation.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
    write_ablation_csv(directory / "ablation.csv", payload)
    write_text(directory / "report.md", ablation_report(payload))
    write_manifest(directory)
    return payload


def ablation_report(payload: dict[str, Any]) -> str:
    lines = [
        "# RouteForge pheromone ablation",
        "",
        "Question: does indexing pheromone by battery state beat a flat `(from, to)` matrix?",
        "",
        "Everything else is held identical, including the instance, cost model, fleet, ant and "
        "iteration budget, and the per-ant random stream. The only difference is the pheromone "
        "tensor's third dimension.",
        "",
        f"- seeds: `{', '.join(str(seed) for seed in payload['seeds'])}`",
        "",
        "| scale | state-aware cheaper | flat cheaper | ties | mean cost delta |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in ("compact", "stress"):
        case = payload[name]
        lines.append(
            f"| {name} ({case['customers']} customers) | {case['stateAwareWins']} | "
            f"{case['flatWins']} | {case['ties']} | {case['meanCostDelta']:+.6f} |"
        )
    lines.extend(
        [
            "",
            f"**Verdict:** {payload['verdict']}.",
            "",
            "A positive mean delta favours the state-aware tensor. This is reported as measured. "
            "See `docs/design-decisions.md` for what the result means for the project's claims.",
            "",
        ]
    )
    return "\n".join(lines)


def write_ablation_csv(path: Path, ablation: dict[str, Any]) -> None:
    fields = ("customers", "seed", "stateAwareCost", "flatPheromoneCost", "costDelta")
    rows = [row for case in ("compact", "stress") for row in ablation[case]["runs"]]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def markdown_report(payload: dict[str, Any]) -> str:
    compact = payload["compact"]
    stress = payload["stress"]
    lines = [
        "# RouteForge benchmark",
        "",
        "A seeded comparison of state-aware ACO against the deterministic greedy baseline.",
        "",
        f"- seed: `{payload['seed']}`",
        "- all values are from synthetic instances; cost, fleet, and iteration budgets are held constant per row",
        "",
        "| scenario | customers | ACO cost | greedy cost | improvement | feasible | served |",
        "|---|---:|---:|---:|---:|:---:|---:|",
    ]
    for name, row in (("compact", compact), ("stress", stress)):
        lines.append(
            f"| {name} | {row['customers']} | {row['acoCost']:.6f} | {row['greedyCost']:.6f} | "
            f"{row['improvementPct']:.4f}% | {'yes' if row['feasible'] else 'no'} | "
            f"{row['servedCustomers']}/{row['customers']} |"
        )
    lines.extend(
        [
            "",
            "The improvement percentage is relative to this row's greedy baseline. It is not a claim that ACO "
            "dominates other solvers or real routing workloads.",
            "",
            "The pheromone ablation lives in its own artifact: `routeforge ablation`.",
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
