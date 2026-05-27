from routeforge import CostModel, Node, ProblemInstance, RoutePlan, SolverConfig, Vehicle, evaluate_plan
from routeforge.evaluation import _State, dynamic_charge_amount
from routeforge.solver import StateAwareACO


def _tiny_instance() -> ProblemInstance:
    return ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 3.0, 0.0, "customer", dry_demand=20.0, recyclable_demand=10.0, latest_minute=300.0),
            Node(2, 6.0, 0.0, "charger"),
            Node(3, 9.0, 0.0, "customer", dry_demand=10.0, recyclable_demand=8.0, latest_minute=300.0),
        ),
        vehicles=(Vehicle(0, battery_capacity_kwh=12.0, dry_capacity_kg=100.0, recyclable_capacity_kg=100.0),),
        costs=CostModel(),
    )


def test_evaluator_replays_charge_and_reports_metrics() -> None:
    instance = _tiny_instance()
    evaluation = evaluate_plan(instance, RoutePlan.from_lists([[0, 1, 2, 3, 0]]))
    assert evaluation.served_customers == (1, 3)
    assert evaluation.charging_kwh > 0
    assert evaluation.distance_km == 18.0
    assert evaluation.feasible
    stops = evaluation.route_details[0]["stops"]
    assert [stop["node_id"] for stop in stops] == [1, 2, 3, 0]
    assert stops[0]["dry_load_kg"] == 20.0
    assert stops[2]["dry_load_kg"] == 30.0
    assert stops[1]["charge_minutes"] > 0


def test_evaluator_exposes_missing_and_capacity_violations() -> None:
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 1.0, 0.0, "customer", dry_demand=200.0, latest_minute=300.0),
            Node(2, 2.0, 0.0, "customer", dry_demand=1.0, latest_minute=300.0),
        ),
        vehicles=(Vehicle(0, dry_capacity_kg=100.0),),
    )
    evaluation = evaluate_plan(instance, [[0, 1, 0]])
    assert evaluation.missing_customers == (2,)
    assert any("dry compartment" in issue for issue in evaluation.violations)
    assert not evaluation.feasible


def test_evaluator_keeps_the_vehicle_assignment_when_an_earlier_vehicle_is_unused() -> None:
    """Route position must not silently replace the vehicle that made the route."""
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 10.0, 0.0, "customer", dry_demand=1.0, latest_minute=300.0),
        ),
        vehicles=(Vehicle(0, battery_capacity_kwh=1.0), Vehicle(1, battery_capacity_kwh=200.0)),
    )
    plan = RoutePlan.from_lists([[0, 1, 0]], vehicle_indices=[1])

    evaluation = evaluate_plan(instance, plan)

    assert evaluation.feasible, evaluation.violations
    assert evaluation.route_details[0]["vehicle_id"] == 1
    assert evaluation.route_details[0]["vehicle_index"] == 1


def test_late_plan_is_ranked_with_a_penalty_but_not_reported_as_feasible() -> None:
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 5.0, 0.0, "customer", latest_minute=1.0),
        ),
        vehicles=(Vehicle(0),),
    )

    evaluation = evaluate_plan(instance, [[0, 1, 0]])

    assert evaluation.lateness_minutes == 6.5
    assert not evaluation.on_time
    assert not evaluation.feasible
    assert evaluation.total_cost > 0
    assert evaluation.as_dict()["on_time"] is False


def test_missing_customer_is_penalized_once_while_its_diagnostic_remains_visible() -> None:
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 0.0, 0.0, "customer"),
            Node(2, 0.0, 0.0, "customer"),
        ),
        vehicles=(Vehicle(0, fixed_cost=1.0),),
        costs=CostModel(
            distance_cost_per_km=0.0,
            energy_cost_per_kwh=0.0,
            lateness_cost_per_minute=0.0,
            missed_customer_penalty=100.0,
        ),
    )

    evaluation = evaluate_plan(instance, [[0, 1, 0]])

    assert evaluation.missing_customers == (2,)
    assert evaluation.violations == ("missing customers: [2]",)
    assert evaluation.total_cost == 101.0


def test_evaluator_reports_battery_safety_and_depletion_as_distinct_failures() -> None:
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 10.0, 0.0, "customer", latest_minute=300.0),
        ),
        vehicles=(Vehicle(0, battery_capacity_kwh=5.0, base_consumption_kwh_per_km=1.0),),
    )

    evaluation = evaluate_plan(instance, [[0, 1, 0]])

    assert not evaluation.feasible
    assert any("battery safety breached" in issue for issue in evaluation.violations)
    assert any("battery depleted at customer 1" in issue for issue in evaluation.violations)


def test_evaluator_preserves_a_fleet_limit_diagnostic_for_an_invalid_assignment() -> None:
    instance = _tiny_instance()
    plan = RoutePlan.from_lists([[0, 1, 0]], vehicle_indices=[99])

    evaluation = evaluate_plan(instance, plan)

    assert not evaluation.feasible
    assert any("fleet limit exceeded" in issue for issue in evaluation.violations)
    assert any("unknown vehicle assignment 99" in issue for issue in evaluation.violations)
    assert evaluation.route_details[0]["vehicle_id"] is None


def test_dynamic_charge_uses_full_charge_only_when_critical_and_otherwise_targets_remaining_work() -> None:
    vehicle = Vehicle(0, battery_capacity_kwh=100.0, base_consumption_kwh_per_km=1.0)
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 30.0, 0.0, "customer", latest_minute=300.0),
            Node(2, 0.0, 0.0, "charger"),
        ),
        vehicles=(vehicle,),
    )

    critical = dynamic_charge_amount(instance, _State(2, 0.0, 20.0), vehicle, [1])
    targeted = dynamic_charge_amount(instance, _State(2, 0.0, 50.0), vehicle, [1])

    assert critical == 80.0
    assert 0.0 < targeted < 50.0
    assert targeted == 40.0


def test_local_search_accepts_an_improving_swap_and_keeps_the_plan_feasible() -> None:
    vehicle = Vehicle(0, battery_capacity_kwh=100.0, base_consumption_kwh_per_km=1.0)
    instance = ProblemInstance(
        nodes=(
            Node(0, 0.0, 0.0, "depot"),
            Node(1, 10.0, 0.0, "customer", latest_minute=300.0),
            Node(2, 0.0, 10.0, "customer", latest_minute=300.0),
            Node(3, 10.0, 10.0, "customer", latest_minute=300.0),
        ),
        vehicles=(vehicle,),
    )
    plan = RoutePlan.from_lists([[0, 1, 2, 3, 0]], vehicle_indices=[0])
    baseline = evaluate_plan(instance, plan)

    solver = StateAwareACO(instance, SolverConfig(ants=1, iterations=1, local_search_passes=2))
    improved_plan, improved = solver._local_search(plan, baseline)

    assert improved.feasible
    assert improved.total_cost < baseline.total_cost
    assert improved_plan.routes != plan.routes
