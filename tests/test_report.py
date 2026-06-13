from pathlib import Path

from routeforge import SolverConfig, generate_instance, solve
from routeforge.report import write_report


def test_report_writes_replayable_artifacts(tmp_path: Path) -> None:
    instance = generate_instance(customers=4, chargers=1, vehicles=2, seed=3)
    result = solve(instance, seed=3, config=SolverConfig(ants=2, iterations=2))
    artifacts = write_report(result, instance, tmp_path / "report")
    assert all(Path(path).exists() for path in artifacts.values())
    html = (tmp_path / "report" / "report.html").read_text(encoding="utf-8")
    assert "RouteForge routing report" in html
    assert "Route map" in html
    assert "Baseline comparison" in html
