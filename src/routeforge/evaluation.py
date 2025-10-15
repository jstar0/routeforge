from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .instance import distance, energy_for, travel_minutes
from .models import Evaluation, ProblemInstance, RoutePlan, Vehicle


@dataclass(slots=True)
class _State:
    location: int
    minute: float
    battery_kwh: float
    dry_load_kg: float = 0.0
    recyclable_load_kg: float = 0.0
    distance_km: float = 0.0
    energy_kwh: float = 0.0
    charging_kwh: float = 0.0
    lateness_minutes: float = 0.0

    @property
    def total_load_kg(self) -> float:
        return self.dry_load_kg + self.recyclable_load_kg


def _required_energy_to_finish(
    instance: ProblemInstance,
    current_id: int,
    remaining_ids: list[int],
    vehicle: Vehicle,
    load_kg: float,
) -> float:
    """Estimate energy for a conservative nearest-neighbour continuation."""

    if not remaining_ids:
        return energy_for(
            distance(instance.node_by_id[current_id], instance.depot), load_kg, vehicle
        )
    pending = set(remaining_ids)
    cursor = instance.node_by_id[current_id]
    estimate = 0.0
    projected_load = load_kg
    while pending:
        next_id = min(pending, key=lambda candidate: distance(cursor, instance.node_by_id[candidate]))
        next_node = instance.node_by_id[next_id]
        estimate += energy_for(distance(cursor, next_node), projected_load, vehicle)
        projected_load += next_node.dry_demand + next_node.recyclable_demand
        cursor = next_node
        pending.remove(next_id)
    estimate += energy_for(distance(cursor, instance.depot), projected_load, vehicle)
    return estimate


def dynamic_charge_amount(
    instance: ProblemInstance,
    state: _State,
    vehicle: Vehicle,
    remaining_customer_ids: list[int],
) -> float:
    """Choose a target charge from remaining work instead of always filling."""

    capacity = vehicle.battery_capacity_kwh
    if state.battery_kwh >= capacity - 1e-9:
        return 0.0
    if state.battery_kwh / capacity < 0.30:
        return capacity - state.battery_kwh
    required = _required_energy_to_finish(
        instance, state.location, remaining_customer_ids, vehicle, state.total_load_kg
    )
    safety_margin = capacity * 0.10
    safety_reserve = capacity * vehicle.safety_battery_fraction
    target = min(capacity, max(state.battery_kwh, required + safety_margin + safety_reserve))
    return max(0.0, target - state.battery_kwh)


def _charge_if_needed(
    instance: ProblemInstance,
    state: _State,
    vehicle: Vehicle,
    remaining_customer_ids: list[int],
) -> None:
    amount = dynamic_charge_amount(instance, state, vehicle, remaining_customer_ids)
    if amount <= 0:
        return
    state.battery_kwh += amount
    state.charging_kwh += amount
    state.minute += amount / vehicle.charging_power_kw * 60.0


def evaluate_plan(instance: ProblemInstance, plan: RoutePlan | list[list[int]]) -> Evaluation:
    """Simulate routes and return costs plus actionable constraint diagnostics.

    Time windows are soft (lateness is priced); compartment capacity and battery
    safety are hard. Charging stations use the same dynamic policy as the solver,
    so a report reflects the behavior a driver would actually execute.
    """

    route_plan = plan if isinstance(plan, RoutePlan) else RoutePlan.from_lists(plan)
    nodes = instance.node_by_id
    depot_id = instance.depot.id
    all_customers = {node.id for node in instance.customers}
    seen_customers: list[int] = []
    violations: list[str] = []
    details: list[dict[str, Any]] = []
    total_distance = 0.0
    total_energy = 0.0
    total_charging = 0.0
    total_lateness = 0.0
    used = 0
    fixed_cost_total = 0.0
    assigned_vehicle_indices: set[int] = set()

    for route_index, (route, vehicle_index) in enumerate(
        zip(route_plan.routes, route_plan.vehicle_indices, strict=True)
    ):
        if len(route) <= 1:
            continue
        route_violations: list[str] = []
        if route[0] != depot_id or route[-1] != depot_id:
            route_violations.append("route must start and end at the depot")
        valid_vehicle_index = 0 <= vehicle_index < len(instance.vehicles)
        if not valid_vehicle_index:
            route_violations.append("fleet limit exceeded")
            route_violations.append(f"unknown vehicle assignment {vehicle_index}")
            # Keep replaying with a known vehicle so the artifact contains all
            # other diagnostics as well; the explicit assignment violations
            # prevent this fallback from being mistaken for a valid evaluation.
            vehicle = instance.vehicles[0]
        else:
            vehicle = instance.vehicles[vehicle_index]
            if vehicle_index in assigned_vehicle_indices:
                route_violations.append(f"vehicle {vehicle.id} assigned to multiple routes")
            assigned_vehicle_indices.add(vehicle_index)
        state = _State(depot_id, 0.0, vehicle.battery_capacity_kwh)
        stops: list[dict[str, Any]] = []
        route_customer_ids = [node_id for node_id in route if node_id in nodes and nodes[node_id].kind == "customer"]
        for position, destination_id in enumerate(route[1:], start=1):
            if destination_id not in nodes:
                route_violations.append(f"unknown node {destination_id}")
                continue
            origin = nodes[state.location]
            destination = nodes[destination_id]
            battery_before = state.battery_kwh
            issues_before = len(route_violations)
            km = distance(origin, destination)
            energy = energy_for(km, state.total_load_kg, vehicle)
            state.battery_kwh -= energy
            state.distance_km += km
            state.energy_kwh += energy
            state.minute += travel_minutes(km, vehicle)
            if state.battery_kwh < vehicle.battery_capacity_kwh * vehicle.safety_battery_fraction - 1e-8:
                route_violations.append(
                    f"battery safety breached before node {destination_id} "
                    f"({state.battery_kwh:.2f} kWh)"
                )
            state.location = destination_id
            arrival = state.minute
            service_start = arrival
            charge_minutes = 0.0
            if destination.kind == "customer":
                seen_customers.append(destination_id)
                if state.battery_kwh < 0:
                    route_violations.append(f"battery depleted at customer {destination_id}")
                service_start = max(state.minute, destination.earliest_minute)
                if service_start > destination.latest_minute:
                    late = service_start - destination.latest_minute
                    state.lateness_minutes += late
                state.minute = service_start + destination.service_minutes
                state.dry_load_kg += destination.dry_demand
                state.recyclable_load_kg += destination.recyclable_demand
                if state.dry_load_kg > vehicle.dry_capacity_kg + 1e-8:
                    route_violations.append(f"dry compartment exceeded at customer {destination_id}")
                if state.recyclable_load_kg > vehicle.recyclable_capacity_kg + 1e-8:
                    route_violations.append(
                        f"recyclable compartment exceeded at customer {destination_id}"
                    )
            elif destination.kind == "charger":
                remaining = [
                    node_id
                    for node_id in route[position:]
                    if node_id in nodes and nodes[node_id].kind == "customer"
                ]
                charge_before = state.charging_kwh
                _charge_if_needed(instance, state, vehicle, remaining)
                charge_minutes = (state.charging_kwh - charge_before) / vehicle.charging_power_kw * 60.0
            elif destination.kind == "depot":
                # A route ends at the depot; loads are unloaded for the next
                # vehicle and no further edge should depend on this state.
                state.dry_load_kg = 0.0
                state.recyclable_load_kg = 0.0
            stops.append(
                {
                    "node_id": destination_id,
                    "sequence": position,
                    "kind": destination.kind,
                    "arrival_minute": round(arrival, 6),
                    "service_start_minute": round(service_start, 6),
                    "departure_minute": round(state.minute, 6),
                    "waiting_minutes": round(max(0.0, service_start - arrival), 6),
                    "charge_minutes": round(charge_minutes, 6),
                    "battery_before_kwh": round(battery_before, 6),
                    "battery_after_kwh": round(state.battery_kwh, 6),
                    "dry_load_kg": round(state.dry_load_kg, 6),
                    "recyclable_load_kg": round(state.recyclable_load_kg, 6),
                    "violations": route_violations[issues_before:],
                }
            )
        for issue in route_violations:
            violations.append(f"route {route_index}: {issue}")
        total_distance += state.distance_km
        total_energy += state.energy_kwh
        total_charging += state.charging_kwh
        total_lateness += state.lateness_minutes
        if route_customer_ids:
            used += 1
            fixed_cost_total += vehicle.fixed_cost
        details.append(
            {
                "vehicle_index": vehicle_index,
                "vehicle_id": vehicle.id if valid_vehicle_index else None,
                "route": list(route),
                "customers": route_customer_ids,
                "distance_km": round(state.distance_km, 6),
                "energy_kwh": round(state.energy_kwh, 6),
                "charging_kwh": round(state.charging_kwh, 6),
                "lateness_minutes": round(state.lateness_minutes, 6),
                "finish_minute": round(state.minute, 6),
                "finish_battery_kwh": round(state.battery_kwh, 6),
                "violations": route_violations,
                "stops": stops,
            }
        )

    duplicates = tuple(sorted(customer for customer in set(seen_customers) if seen_customers.count(customer) > 1))
    missing = tuple(sorted(all_customers - set(seen_customers)))
    # Coverage is priced per customer. Preserve its summaries in the diagnostic
    # list, but do not charge them a second time as generic route violations.
    route_violation_count = len(violations)
    if duplicates:
        violations.append(f"duplicate customers: {list(duplicates)}")
    if missing:
        violations.append(f"missing customers: {list(missing)}")
    penalty = (
        total_lateness * instance.costs.lateness_cost_per_minute
        + (len(missing) + len(duplicates)) * instance.costs.missed_customer_penalty
        + route_violation_count * instance.costs.missed_customer_penalty
    )
    total_cost = (
        fixed_cost_total
        + total_distance * instance.costs.distance_cost_per_km
        + total_charging * instance.costs.energy_cost_per_kwh
        + penalty
    )
    return Evaluation(
        total_cost=total_cost,
        distance_km=total_distance,
        energy_kwh=total_energy,
        charging_kwh=total_charging,
        lateness_minutes=total_lateness,
        vehicles_used=used,
        served_customers=tuple(sorted(set(seen_customers))),
        missing_customers=missing,
        duplicate_customers=duplicates,
        violations=tuple(violations),
        route_details=tuple(details),
    )
