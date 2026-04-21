import json
from pathlib import Path

from routeforge import benchmark_payload, write_benchmark


def test_benchmark_compares_feasible_aco_and_greedy_plans() -> None:
    payload = benchmark_payload(17)
    assert payload["compact"]["feasible"]
    assert payload["stress"]["feasible"]
    assert payload["compact"]["servedCustomers"] == 12
    assert payload["stress"]["servedCustomers"] == 30
    assert payload["compact"]["improvementPct"] > 0


def test_benchmark_is_deterministic() -> None:
    assert benchmark_payload(29) == benchmark_payload(29)


def test_benchmark_writer_emits_hash_bound_artifacts(tmp_path: Path) -> None:
    write_benchmark(tmp_path, 17)
    assert {path.name for path in tmp_path.iterdir()} == {
        "benchmark.json",
        "runs.csv",
        "report.md",
        "manifest.json",
    }
    payload = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert payload["boundary"]["synthetic"] is True
    assert {item["name"] for item in manifest["files"]} == {"benchmark.json", "runs.csv", "report.md"}


def test_benchmark_artifacts_repeat_byte_for_byte(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    write_benchmark(first, 41)
    write_benchmark(second, 41)
    for name in ("benchmark.json", "runs.csv", "report.md", "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()
