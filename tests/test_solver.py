from routeforge import SolverConfig, generate_instance, solve


def test_solver_covers_generated_customers_and_is_seeded() -> None:
    instance = generate_instance(customers=8, chargers=2, vehicles=3, seed=8)
    config = SolverConfig(ants=6, iterations=8, local_search_passes=1)
    first = solve(instance, seed=8, config=config)
    second = solve(instance, seed=8, config=config)
    assert first.plan.as_dict() == second.plan.as_dict()
    assert first.evaluation.as_dict() == second.evaluation.as_dict()
    assert set(first.evaluation.served_customers) == {node.id for node in instance.customers}
    assert first.evaluation.feasible
    assert len(first.history) == 8


def test_aco_keeps_a_baseline_comparison() -> None:
    instance = generate_instance(customers=5, chargers=1, vehicles=2, seed=4)
    result = solve(instance, seed=4, config=SolverConfig(ants=3, iterations=3))
    assert result.baseline_plan.routes
    assert result.baseline_evaluation.total_cost >= 0
    assert result.evaluation.total_cost <= result.baseline_evaluation.total_cost + 1e-6
