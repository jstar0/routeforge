from __future__ import annotations

import json
from pathlib import Path

from routeforge.instance import load_instance_json
from routeforge.solver import SolverConfig, solve

CASES = Path(__file__).parents[1] / "examples" / "cases"
CONFIG = SolverConfig(iterations=30, ants=8)


def test_normal_case_is_feasible_without_charging() -> None:
    result = solve(load_instance_json(CASES / "normal-feasible.json"), seed=42, config=CONFIG)
    assert result.evaluation.feasible
    assert result.evaluation.charging_kwh == 0
    assert result.evaluation.missing_customers == ()


def test_charging_case_visits_charger_and_is_feasible() -> None:
    result = solve(load_instance_json(CASES / "requires-charging.json"), seed=42, config=CONFIG)
    stops = [stop for detail in result.evaluation.route_details for stop in detail["stops"]]
    assert result.evaluation.feasible
    assert result.evaluation.charging_kwh > 0
    assert sum(stop["kind"] == "charger" for stop in stops) >= 1
    assert any(stop["charge_minutes"] > 0 for stop in stops)


def test_tight_case_is_reported_as_infeasible_not_proven_unsatisfiable() -> None:
    result = solve(load_instance_json(CASES / "tight-search-infeasible.json"), seed=42, config=CONFIG)
    serialized = json.dumps(result.as_dict())
    assert not result.evaluation.feasible
    assert "unsatisfiable" not in serialized.lower()
    assert result.evaluation.violations
