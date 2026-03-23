"""The pheromone ablation must be able to contradict the project's own idea."""

from dataclasses import replace
from pathlib import Path

from routeforge import ablation_payload, generate_instance, write_ablation
from routeforge.benchmark import DEFAULT_CONFIG
from routeforge.solver import StateAwareACO, solve

# Two seeds keep this suite fast; the recorded eight-seed result lives in the
# artifact written by `routeforge ablation`.
SEEDS = (17, 29)


def test_flat_variant_collapses_the_pheromone_tensor() -> None:
    instance = generate_instance(customers=12, chargers=3, vehicles=3, scale_km=40.0, seed=17)
    assert StateAwareACO(instance, DEFAULT_CONFIG).levels == DEFAULT_CONFIG.battery_levels
    assert StateAwareACO(instance, replace(DEFAULT_CONFIG, state_aware=False)).levels == 1


def test_flat_variant_is_a_real_deterministic_run() -> None:
    instance = generate_instance(customers=12, chargers=3, vehicles=3, scale_km=40.0, seed=17)
    flat_config = replace(DEFAULT_CONFIG, state_aware=False)
    first = solve(instance, seed=17, config=flat_config)
    second = solve(instance, seed=17, config=flat_config)
    assert first.method == "flat-pheromone-aco"
    assert first.evaluation.feasible
    assert first.evaluation.total_cost == second.evaluation.total_cost


def test_counts_and_deltas_are_internally_consistent() -> None:
    payload = ablation_payload(SEEDS)
    for case in ("compact", "stress"):
        entry = payload[case]
        assert entry["seeds"] == list(SEEDS)
        assert entry["stateAwareWins"] + entry["flatWins"] + entry["ties"] == len(SEEDS)
        for run in entry["runs"]:
            assert run["costDelta"] == round(run["flatPheromoneCost"] - run["stateAwareCost"], 8)


def test_verdict_follows_the_counts_rather_than_the_hypothesis() -> None:
    payload = ablation_payload(SEEDS)
    wins = payload["compact"]["stateAwareWins"] + payload["stress"]["stateAwareWins"]
    total = len(SEEDS) * 2
    supported = wins * 2 > total + 1
    assert supported == payload["verdict"].startswith("state-aware cheaper")
    if not supported:
        assert "not supported" in payload["verdict"]


def test_ablation_is_deterministic() -> None:
    assert ablation_payload(SEEDS) == ablation_payload(SEEDS)


def test_writer_emits_hash_bound_ablation_artifacts(tmp_path: Path) -> None:
    write_ablation(tmp_path, SEEDS)
    assert {path.name for path in tmp_path.iterdir()} == {
        "ablation.json",
        "ablation.csv",
        "report.md",
        "manifest.json",
    }
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Verdict:" in report
