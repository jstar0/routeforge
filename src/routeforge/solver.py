from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass

from .evaluation import dynamic_charge_amount, evaluate_plan
from .instance import battery_bucket, distance, energy_for, travel_minutes
from .models import Evaluation, ProblemInstance, RoutePlan, SolveResult, Vehicle


@dataclass(frozen=True, slots=True)
class SolverConfig:
    ants: int = 18
    iterations: int = 35
    alpha: float = 1.0
    beta: float = 3.0
    evaporation: float = 0.18
    elite_weight: float = 2.0
    battery_levels: int = 4
    local_search_passes: int = 2
    # When False the pheromone tensor collapses to a plain (from, to) matrix.
    # This exists so the battery-state dimension can be ablated against an
    # otherwise identical run rather than asserted.
    state_aware: bool = True

    def __post_init__(self) -> None:
        if self.ants < 1 or self.iterations < 1 or self.battery_levels < 2:
            raise ValueError("ants, iterations, and battery_levels must be positive")
        if not 0 < self.evaporation < 1:
            raise ValueError("evaporation must be between 0 and 1")
        if self.alpha < 0 or self.beta < 0 or self.elite_weight < 0:
            raise ValueError("ACO weights cannot be negative")


@dataclass(slots=True)
class _BuildState:
    location: int
    minute: float
    battery_kwh: float
    dry_load_kg: float = 0.0
    recyclable_load_kg: float = 0.0

    @property
    def load_kg(self) -> float:
        return self.dry_load_kg + self.recyclable_load_kg


class _Builder:
    def __init__(self, instance: ProblemInstance, config: SolverConfig) -> None:
        self.instance = instance
        self.config = config
        self.nodes = instance.node_by_id
        self.depot_id = instance.depot.id

    def _edge(self, state: _BuildState, destination_id: int, vehicle: Vehicle) -> tuple[float, float]:
        km = distance(self.nodes[state.location], self.nodes[destination_id])
        energy = energy_for(km, state.load_kg, vehicle)
        return km, energy

    def _customer_feasible(
        self, state: _BuildState, customer_id: int, vehicle: Vehicle
    ) -> bool:
        customer = self.nodes[customer_id]
        _km, energy = self._edge(state, customer_id, vehicle)
        remaining_battery = state.battery_kwh - energy
        reserve = vehicle.battery_capacity_kwh * vehicle.safety_battery_fraction
        if remaining_battery < reserve:
            return False
        if state.dry_load_kg + customer.dry_demand > vehicle.dry_capacity_kg:
            return False
        if state.recyclable_load_kg + customer.recyclable_demand > vehicle.recyclable_capacity_kg:
            return False
        # Getting there is not enough: the vehicle also has to get home, and this
        # is a collection problem so it carries more weight on the way back.
        # Without this the builder produced plans that stranded a vehicle at its
        # last customer, and the evaluator then charged a 10,000 penalty for a
        # violation the builder had already been told about.
        exit_load = state.load_kg + customer.dry_demand + customer.recyclable_demand
        return_km = distance(self.nodes[customer_id], self.nodes[self.depot_id])
        return_energy = energy_for(return_km, exit_load, vehicle)
        can_reach_depot = remaining_battery - return_energy >= reserve
        if not can_reach_depot:
            escape = self._reachable_charger_from(
                customer_id, remaining_battery, exit_load, vehicle
            )
            if escape is None:
                return False
        arrival = state.minute + travel_minutes(_km, vehicle)
        service_start = max(arrival, customer.earliest_minute)
        return service_start <= customer.latest_minute

    def _reachable_charger_from(
        self, origin_id: int, battery_kwh: float, load_kg: float, vehicle: Vehicle
    ) -> int | None:
        """A charger the vehicle could still reach from ``origin_id``.

        Used to keep the return-leg check from rejecting a customer that is
        perfectly serviceable because a charger sits between it and the depot.
        """
        reserve = vehicle.battery_capacity_kwh * vehicle.safety_battery_fraction
        best: tuple[float, int] | None = None
        for charger in self.instance.chargers:
            km = distance(self.nodes[origin_id], self.nodes[charger.id])
            if battery_kwh - energy_for(km, load_kg, vehicle) >= reserve:
                if best is None or km < best[0]:
                    best = (km, charger.id)
        return None if best is None else best[1]

    def _advance_customer(self, state: _BuildState, customer_id: int, vehicle: Vehicle) -> None:
        customer = self.nodes[customer_id]
        km, energy = self._edge(state, customer_id, vehicle)
        state.battery_kwh -= energy
        state.minute += travel_minutes(km, vehicle)
        state.minute = max(state.minute, customer.earliest_minute) + customer.service_minutes
        state.dry_load_kg += customer.dry_demand
        state.recyclable_load_kg += customer.recyclable_demand
        state.location = customer_id

    def _reachable_charger(self, state: _BuildState, vehicle: Vehicle, last_id: int | None) -> int | None:
        candidates = []
        for charger in self.instance.chargers:
            if charger.id == last_id:
                continue
            _km, energy = self._edge(state, charger.id, vehicle)
            if state.battery_kwh - energy >= vehicle.battery_capacity_kwh * vehicle.safety_battery_fraction:
                candidates.append(charger.id)
        return min(
            candidates,
            key=lambda node_id: distance(self.nodes[state.location], self.nodes[node_id]),
            default=None,
        )

    def _advance_charger(
        self, state: _BuildState, charger_id: int, vehicle: Vehicle, remaining: list[int]
    ) -> float:
        km, energy = self._edge(state, charger_id, vehicle)
        state.battery_kwh -= energy
        state.minute += travel_minutes(km, vehicle)
        state.location = charger_id
        # Use the same policy as the evaluator. A tiny adapter keeps the
        # state-aware decision shared without exposing evaluator internals.
        from .evaluation import _State

        adapter = _State(
            charger_id,
            state.minute,
            state.battery_kwh,
            state.dry_load_kg,
            state.recyclable_load_kg,
        )
        amount = dynamic_charge_amount(self.instance, adapter, vehicle, remaining)
        state.battery_kwh += amount
        state.minute += amount / vehicle.charging_power_kw * 60.0
        return amount

    def _close_route(self, route: list[int], state: _BuildState, vehicle: Vehicle) -> None:
        """Return to the depot, charging on the way if the battery cannot make it.

        The builder admits a customer when the depot is reachable *or* a charger
        is, so closing the route has to honour the second case. Appending the
        depot unconditionally is what previously stranded a vehicle at its last
        stop and turned the whole plan infeasible.
        """
        reserve = vehicle.battery_capacity_kwh * vehicle.safety_battery_fraction
        for _hop in range(max(1, len(self.instance.chargers))):
            _km, energy = self._edge(state, self.depot_id, vehicle)
            if state.battery_kwh - energy >= reserve:
                break
            charger = self._reachable_charger_from(
                state.location, state.battery_kwh, state.load_kg, vehicle
            )
            if charger is None or charger == state.location:
                break
            route.append(charger)
            self._advance_charger(state, charger, vehicle, [])
        route.append(self.depot_id)

    def _greedy_customer(self, state: _BuildState, candidates: list[int], vehicle: Vehicle) -> int:
        def key(customer_id: int) -> tuple[float, float, int]:
            customer = self.nodes[customer_id]
            km, _energy = self._edge(state, customer_id, vehicle)
            urgency = max(0.0, customer.latest_minute - state.minute)
            return (km + urgency * 0.01, -customer.dry_demand - customer.recyclable_demand, customer_id)

        return min(candidates, key=key)

    def greedy_plan(self) -> RoutePlan:
        remaining = {customer.id for customer in self.instance.customers}
        routes: list[list[int]] = []
        vehicle_indices: list[int] = []
        for vehicle_index, vehicle in enumerate(self.instance.vehicles):
            if not remaining:
                break
            route = [self.depot_id]
            state = _BuildState(self.depot_id, 0.0, vehicle.battery_capacity_kwh)
            last_charger: int | None = None
            charger_moves = 0
            while remaining:
                candidates = [
                    customer_id
                    for customer_id in sorted(remaining)
                    if self._customer_feasible(state, customer_id, vehicle)
                ]
                if candidates:
                    chosen = self._greedy_customer(state, candidates, vehicle)
                    route.append(chosen)
                    self._advance_customer(state, chosen, vehicle)
                    remaining.remove(chosen)
                    last_charger = None
                    continue
                charger = self._reachable_charger(state, vehicle, last_charger)
                if charger is None or charger_moves >= max(1, len(self.instance.chargers) * 2):
                    break
                route.append(charger)
                amount = self._advance_charger(state, charger, vehicle, sorted(remaining))
                charger_moves += 1
                last_charger = charger
                if amount <= 1e-9 and charger_moves > 1:
                    break
            self._close_route(route, state, vehicle)
            if len(route) > 2:
                routes.append(route)
                vehicle_indices.append(vehicle_index)
        if remaining:
            # Preserve coverage in the returned artifact; the evaluator will
            # expose any fleet/capacity issue instead of silently dropping work.
            routes.append([self.depot_id, *sorted(remaining), self.depot_id])
            vehicle_indices.append(len(self.instance.vehicles))
        return RoutePlan.from_lists(routes, vehicle_indices)


class StateAwareACO(_Builder):
    """Constraint-driven ACO with a battery-state pheromone tensor."""

    def __init__(self, instance: ProblemInstance, config: SolverConfig | None = None) -> None:
        super().__init__(instance, config or SolverConfig())
        self.node_ids = [node.id for node in instance.nodes]
        self.index = {node_id: index for index, node_id in enumerate(self.node_ids)}
        self.levels = self.config.battery_levels if self.config.state_aware else 1
        self.pheromone = [
            [[1.0 for _level in range(self.levels)] for _to in self.node_ids]
            for _from in self.node_ids
        ]

    def _bucket(self, battery_kwh: float, vehicle: Vehicle) -> int:
        """Discretised battery state, or a single bucket when ablated."""

        if not self.config.state_aware:
            return 0
        return battery_bucket(battery_kwh, vehicle, self.config.battery_levels)

    def _tau(self, origin: int, destination: int, bucket: int) -> float:
        return self.pheromone[self.index[origin]][self.index[destination]][bucket]

    def _candidate_weight(
        self, state: _BuildState, candidate_id: int, vehicle: Vehicle, rng: random.Random
    ) -> float:
        km, _energy = self._edge(state, candidate_id, vehicle)
        customer = self.nodes[candidate_id]
        urgency = 1.0 / max(1.0, customer.latest_minute - state.minute)
        demand = 1.0 + (customer.dry_demand + customer.recyclable_demand) / 400.0
        heuristic = (1.0 / (km + 0.25)) * (1.0 + 30.0 * urgency) * demand
        bucket = self._bucket(state.battery_kwh, vehicle)
        pheromone = self._tau(state.location, candidate_id, bucket)
        jitter = 1.0 + rng.random() * 0.02
        return max(1e-12, (pheromone**self.config.alpha) * (heuristic**self.config.beta) * jitter)

    def _choose_customer(
        self, state: _BuildState, candidates: list[int], vehicle: Vehicle, rng: random.Random
    ) -> int:
        weighted = [(candidate, self._candidate_weight(state, candidate, vehicle, rng)) for candidate in candidates]
        total = sum(weight for _candidate, weight in weighted)
        draw = rng.random() * total
        running = 0.0
        for candidate, weight in weighted:
            running += weight
            if running >= draw:
                return candidate
        return weighted[-1][0]

    def _construct_ant(self, rng: random.Random) -> tuple[RoutePlan, list[tuple[int, int, int]]]:
        remaining = {customer.id for customer in self.instance.customers}
        routes: list[list[int]] = []
        vehicle_indices: list[int] = []
        traversed: list[tuple[int, int, int]] = []
        for vehicle_index, vehicle in enumerate(self.instance.vehicles):
            if not remaining:
                break
            route = [self.depot_id]
            state = _BuildState(self.depot_id, 0.0, vehicle.battery_capacity_kwh)
            last_charger: int | None = None
            charger_moves = 0
            while remaining:
                candidates = [
                    customer_id
                    for customer_id in remaining
                    if self._customer_feasible(state, customer_id, vehicle)
                ]
                if candidates:
                    chosen = self._choose_customer(state, sorted(candidates), vehicle, rng)
                    bucket = self._bucket(state.battery_kwh, vehicle)
                    traversed.append((state.location, chosen, bucket))
                    route.append(chosen)
                    self._advance_customer(state, chosen, vehicle)
                    remaining.remove(chosen)
                    last_charger = None
                    continue
                charger = self._reachable_charger(state, vehicle, last_charger)
                if charger is None or charger_moves >= max(1, len(self.instance.chargers) * 2):
                    break
                bucket = self._bucket(state.battery_kwh, vehicle)
                traversed.append((state.location, charger, bucket))
                route.append(charger)
                amount = self._advance_charger(state, charger, vehicle, sorted(remaining))
                charger_moves += 1
                last_charger = charger
                if amount <= 1e-9 and charger_moves > 1:
                    break
            if route[-1] != self.depot_id:
                closing_start = len(route)
                self._close_route(route, state, vehicle)
                previous = route[closing_start - 1]
                for node_id in route[closing_start:]:
                    traversed.append((previous, node_id, self._bucket(state.battery_kwh, vehicle)))
                    previous = node_id
            if len(route) > 2:
                routes.append(route)
                vehicle_indices.append(vehicle_index)
        if remaining:
            routes.append([self.depot_id, *sorted(remaining), self.depot_id])
            vehicle_indices.append(len(self.instance.vehicles))
        return RoutePlan.from_lists(routes, vehicle_indices), traversed

    def _update_pheromone(
        self, candidates: list[tuple[RoutePlan, Evaluation, list[tuple[int, int, int]]]], best: Evaluation
    ) -> None:
        for origin_index in range(len(self.pheromone)):
            for destination_index in range(len(self.pheromone[origin_index])):
                for level in range(self.levels):
                    value = self.pheromone[origin_index][destination_index][level]
                    self.pheromone[origin_index][destination_index][level] = max(
                        0.01, value * (1.0 - self.config.evaporation)
                    )
        ranked = sorted(candidates, key=lambda item: item[1].total_cost)[: max(1, len(candidates) // 5)]
        for _plan, evaluation, edges in ranked:
            if not math.isfinite(evaluation.total_cost):
                continue
            deposit = 80.0 / (1.0 + evaluation.total_cost)
            if evaluation.feasible and evaluation.total_cost <= best.total_cost + 1e-9:
                deposit *= self.config.elite_weight
            for origin, destination, level in edges:
                origin_index = self.index[origin]
                destination_index = self.index[destination]
                self.pheromone[origin_index][destination_index][level] = min(
                    100.0,
                    self.pheromone[origin_index][destination_index][level] + deposit,
                )

    def _local_search(self, plan: RoutePlan, baseline: Evaluation) -> tuple[RoutePlan, Evaluation]:
        current_plan, current_eval = plan, baseline
        for _pass in range(self.config.local_search_passes):
            improved = False
            for route_index, route in enumerate(current_plan.routes):
                customer_positions = [
                    position
                    for position, node_id in enumerate(route)
                    if node_id in self.nodes and self.nodes[node_id].kind == "customer"
                ]
                for left_index, left in enumerate(customer_positions):
                    for right in customer_positions[left_index + 1 :]:
                        candidate_routes = [list(item) for item in current_plan.routes]
                        candidate_routes[route_index][left], candidate_routes[route_index][right] = (
                            candidate_routes[route_index][right],
                            candidate_routes[route_index][left],
                        )
                        candidate_plan = RoutePlan.from_lists(candidate_routes, current_plan.vehicle_indices)
                        candidate_eval = evaluate_plan(self.instance, candidate_plan)
                        if candidate_eval.total_cost + 1e-9 < current_eval.total_cost:
                            current_plan, current_eval = candidate_plan, candidate_eval
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
            if not improved:
                break
        return current_plan, current_eval

    def solve(
        self,
        *,
        seed: int = 42,
        on_iteration: Callable[[int, Evaluation, Evaluation], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> SolveResult:
        baseline_builder = _Builder(self.instance, self.config)
        baseline_plan = baseline_builder.greedy_plan()
        baseline_eval = evaluate_plan(self.instance, baseline_plan)
        best_plan, best_eval = baseline_plan, baseline_eval
        history: list[float] = []
        complete = True
        for iteration in range(self.config.iterations):
            candidates: list[tuple[RoutePlan, Evaluation, list[tuple[int, int, int]]]] = []
            for ant in range(self.config.ants):
                rng = random.Random(seed + iteration * 1_000_003 + ant * 9_973)
                plan, edges = self._construct_ant(rng)
                evaluation = evaluate_plan(self.instance, plan)
                candidates.append((plan, evaluation, edges))
                if evaluation.total_cost < best_eval.total_cost:
                    best_plan, best_eval = plan, evaluation
            self._update_pheromone(candidates, best_eval)
            history.append(best_eval.total_cost)
            if on_iteration is not None:
                on_iteration(iteration + 1, best_eval, baseline_eval)
            # The cancellation probe deliberately happens after pheromone
            # update and history append.  A cancelled result therefore always
            # contains the last *complete* ACO iteration, never a partly-built
            # ant population presented as a plan.
            if should_cancel is not None and should_cancel():
                complete = False
                break
        if complete:
            best_plan, best_eval = self._local_search(best_plan, best_eval)
        return SolveResult(
            method="state-aware-aco" if self.config.state_aware else "flat-pheromone-aco",
            seed=seed,
            iterations=self.config.iterations,
            ants=self.config.ants,
            plan=best_plan,
            evaluation=best_eval,
            baseline_plan=baseline_plan,
            baseline_evaluation=baseline_eval,
            history=tuple(history),
            complete=complete,
        )


def solve(
    instance: ProblemInstance,
    *,
    seed: int = 42,
    config: SolverConfig | None = None,
    on_iteration: Callable[[int, Evaluation, Evaluation], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SolveResult:
    return StateAwareACO(instance, config).solve(
        seed=seed,
        on_iteration=on_iteration,
        should_cancel=should_cancel,
    )


def solve_greedy(instance: ProblemInstance, *, seed: int = 42) -> SolveResult:
    """Evaluate the deterministic constructive baseline as its own plan."""

    builder = _Builder(instance, SolverConfig(iterations=1, ants=1))
    plan = builder.greedy_plan()
    evaluation = evaluate_plan(instance, plan)
    return SolveResult(
        method="greedy",
        seed=seed,
        iterations=0,
        ants=0,
        plan=plan,
        evaluation=evaluation,
        baseline_plan=plan,
        baseline_evaluation=evaluation,
        history=(),
    )
