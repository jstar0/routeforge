"""The builder must not admit a customer it cannot come back from.

`_customer_feasible` originally checked only that the vehicle could *reach* the
next customer while holding its safety reserve. Nothing reserved energy for the
trip home, and this is a collection problem, so the vehicle is heaviest on that
last leg. Plans therefore stranded a vehicle at its final stop, the evaluator
charged a 10,000 infeasibility penalty, and the headline benchmark comparison
was measuring that penalty rather than route quality.
"""

from __future__ import annotations

import pytest

from routeforge import evaluate_plan, generate_instance
from routeforge.benchmark import ABLATION_SEEDS, DEFAULT_CONFIG, STRESS_CONFIG
from routeforge.solver import StateAwareACO, _Builder

SCALES = (("compact", 12, 3, 3, DEFAULT_CONFIG), ("stress", 30, 5, 5, STRESS_CONFIG))


@pytest.mark.parametrize("scale", SCALES, ids=[case[0] for case in SCALES])
@pytest.mark.parametrize("seed", ABLATION_SEEDS)
def test_greedy_baseline_returns_to_the_depot(scale, seed: int) -> None:
    _name, customers, chargers, vehicles, config = scale
    instance = generate_instance(
        customers=customers, chargers=chargers, vehicles=vehicles, scale_km=40.0, seed=seed
    )
    evaluation = evaluate_plan(instance, _Builder(instance, config).greedy_plan())
    assert evaluation.feasible, evaluation.violations
    assert not evaluation.violations


@pytest.mark.parametrize("seed", (17, 29, 41))
def test_aco_plan_also_returns_to_the_depot(seed: int) -> None:
    instance = generate_instance(customers=12, chargers=3, vehicles=3, scale_km=40.0, seed=seed)
    result = StateAwareACO(instance, DEFAULT_CONFIG).solve(seed=seed)
    assert result.evaluation.feasible, result.evaluation.violations


def test_every_route_starts_and_ends_at_the_depot() -> None:
    instance = generate_instance(customers=30, chargers=5, vehicles=5, scale_km=40.0, seed=17)
    for plan in (
        _Builder(instance, STRESS_CONFIG).greedy_plan(),
        StateAwareACO(instance, STRESS_CONFIG).solve(seed=17).plan,
    ):
        for route in plan.routes:
            assert route[0] == instance.depot.id
            assert route[-1] == instance.depot.id


def test_improvement_over_greedy_is_a_routing_result_not_a_penalty() -> None:
    """Guards the honesty of the headline number.

    If the greedy baseline is infeasible, "improvement" is dominated by a fixed
    10,000 penalty and says nothing about routing. This test fails if that ever
    comes back.
    """
    instance = generate_instance(customers=12, chargers=3, vehicles=3, scale_km=40.0, seed=17)
    greedy = evaluate_plan(instance, _Builder(instance, DEFAULT_CONFIG).greedy_plan())
    aco = StateAwareACO(instance, DEFAULT_CONFIG).solve(seed=17).evaluation

    assert greedy.feasible
    assert aco.feasible
    improvement = (greedy.total_cost - aco.total_cost) / greedy.total_cost * 100.0
    # A plausible routing gain, not a penalty artifact in the nineties.
    assert 0.0 <= improvement < 80.0
