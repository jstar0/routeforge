from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

NodeKind = Literal["depot", "customer", "charger"]


@dataclass(frozen=True, slots=True)
class Node:
    """A depot, pickup customer, or charging station in a 2-D instance."""

    id: int
    x: float
    y: float
    kind: NodeKind
    dry_demand: float = 0.0
    recyclable_demand: float = 0.0
    earliest_minute: float = 0.0
    latest_minute: float = 1_440.0
    service_minutes: float = 0.0

    def __post_init__(self) -> None:
        numeric = (
            self.x, self.y, self.dry_demand, self.recyclable_demand,
            self.earliest_minute, self.latest_minute, self.service_minutes,
        )
        if type(self.id) is not int or self.id < 0 or not all(math.isfinite(float(v)) for v in numeric):
            raise ValueError("node id and coordinates must be finite and non-negative")
        if self.kind not in {"depot", "customer", "charger"}:
            raise ValueError(f"unsupported node kind: {self.kind}")
        if self.dry_demand < 0 or self.recyclable_demand < 0:
            raise ValueError("demands cannot be negative")
        if self.earliest_minute > self.latest_minute:
            raise ValueError("time window must be ascending")
        if self.service_minutes < 0:
            raise ValueError("service time cannot be negative")
        if self.kind != "customer" and (self.dry_demand or self.recyclable_demand):
            raise ValueError("only customer nodes may have demand")


@dataclass(frozen=True, slots=True)
class Vehicle:
    """Vehicle with two independent payload compartments and a battery."""

    id: int
    battery_capacity_kwh: float = 120.0
    dry_capacity_kg: float = 1_200.0
    recyclable_capacity_kg: float = 1_000.0
    base_consumption_kwh_per_km: float = 0.75
    load_consumption_kwh_per_kg_km: float = 0.00020
    speed_kmph: float = 40.0
    safety_battery_fraction: float = 0.20
    charging_power_kw: float = 90.0
    fixed_cost: float = 280.0

    def __post_init__(self) -> None:
        positive = (
            self.battery_capacity_kwh,
            self.dry_capacity_kg,
            self.recyclable_capacity_kg,
            self.base_consumption_kwh_per_km,
            self.speed_kmph,
            self.charging_power_kw,
            self.fixed_cost,
        )
        all_values = (*positive, self.load_consumption_kwh_per_kg_km, self.safety_battery_fraction)
        if type(self.id) is not int or self.id < 0 or not all(math.isfinite(float(value)) for value in all_values):
            raise ValueError("vehicle id and numeric fields must be finite")
        if any(float(value) <= 0 for value in positive):
            raise ValueError("vehicle capacities, rates, speed, and costs must be positive")
        if self.load_consumption_kwh_per_kg_km < 0:
            raise ValueError("load consumption factor cannot be negative")
        if not 0 < self.safety_battery_fraction < 1:
            raise ValueError("safety battery fraction must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class CostModel:
    distance_cost_per_km: float = 1.20
    energy_cost_per_kwh: float = 0.80
    lateness_cost_per_minute: float = 8.0
    missed_customer_penalty: float = 10_000.0

    def __post_init__(self) -> None:
        values = (
            self.distance_cost_per_km,
            self.energy_cost_per_kwh,
            self.lateness_cost_per_minute,
            self.missed_customer_penalty,
        )
        if not all(math.isfinite(float(value)) for value in values) or any(
            float(value) < 0
            for value in (
                self.distance_cost_per_km,
                self.energy_cost_per_kwh,
                self.lateness_cost_per_minute,
                self.missed_customer_penalty,
            )
        ):
            raise ValueError("cost coefficients cannot be negative")


@dataclass(frozen=True, slots=True)
class ProblemInstance:
    nodes: tuple[Node, ...]
    vehicles: tuple[Vehicle, ...]
    costs: CostModel = field(default_factory=CostModel)

    def __post_init__(self) -> None:
        if not self.nodes:
            raise ValueError("an instance needs at least a depot")
        ids = [node.id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("node IDs must be unique")
        if sum(node.kind == "depot" for node in self.nodes) != 1:
            raise ValueError("an instance needs exactly one depot")
        if not self.vehicles:
            raise ValueError("an instance needs at least one vehicle")
        vehicle_ids = [vehicle.id for vehicle in self.vehicles]
        if len(set(vehicle_ids)) != len(vehicle_ids):
            raise ValueError("vehicle IDs must be unique")

    @property
    def depot(self) -> Node:
        return next(node for node in self.nodes if node.kind == "depot")

    @property
    def node_by_id(self) -> dict[int, Node]:
        return {node.id: node for node in self.nodes}

    @property
    def customers(self) -> tuple[Node, ...]:
        return tuple(node for node in self.nodes if node.kind == "customer")

    @property
    def chargers(self) -> tuple[Node, ...]:
        return tuple(node for node in self.nodes if node.kind == "charger")

    def as_dict(self) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": node.id,
                    "x": node.x,
                    "y": node.y,
                    "kind": node.kind,
                    "dry_demand": node.dry_demand,
                    "recyclable_demand": node.recyclable_demand,
                    "earliest_minute": node.earliest_minute,
                    "latest_minute": node.latest_minute,
                    "service_minutes": node.service_minutes,
                }
                for node in self.nodes
            ],
            "vehicles": [asdict(vehicle) for vehicle in self.vehicles],
            "costs": asdict(self.costs),
        }


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """A route list with the source vehicle for every route.

    Omitting assignments preserves the original sequential interpretation for
    hand-authored ``list[list[int]]`` input. Solver-produced plans always carry
    their explicit assignment.
    """

    routes: tuple[tuple[int, ...], ...]
    # A route must retain the vehicle that constructed it. Route position is not
    # enough because a solver may leave an earlier vehicle unused.
    vehicle_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not self.vehicle_indices:
            object.__setattr__(self, "vehicle_indices", tuple(range(len(self.routes))))
        if len(self.routes) != len(self.vehicle_indices):
            raise ValueError("each route needs exactly one vehicle assignment")

    @classmethod
    def from_lists(
        cls, routes: list[list[int]], vehicle_indices: list[int] | tuple[int, ...] | None = None
    ) -> RoutePlan:
        route_tuples = tuple(tuple(int(node) for node in route) for route in routes)
        assignments = () if vehicle_indices is None else tuple(int(index) for index in vehicle_indices)
        return cls(route_tuples, assignments)

    def as_dict(self) -> dict[str, Any]:
        return {
            "routes": [list(route) for route in self.routes],
            "vehicle_indices": list(self.vehicle_indices),
        }


@dataclass(frozen=True, slots=True)
class Evaluation:
    total_cost: float
    distance_km: float
    energy_kwh: float
    charging_kwh: float
    lateness_minutes: float
    vehicles_used: int
    served_customers: tuple[int, ...]
    missing_customers: tuple[int, ...]
    duplicate_customers: tuple[int, ...]
    violations: tuple[str, ...]
    route_details: tuple[dict[str, Any], ...] = ()

    @property
    def on_time(self) -> bool:
        return self.lateness_minutes <= 1e-8

    @property
    def feasible(self) -> bool:
        """Whether the plan satisfies every modeled operating constraint.

        Lateness remains in the objective as a soft ranking signal, but a late
        plan cannot be presented as feasible in a report or benchmark.
        """
        return self.on_time and not self.violations and not self.missing_customers and not self.duplicate_customers

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_cost": round(self.total_cost, 6),
            "distance_km": round(self.distance_km, 6),
            "energy_kwh": round(self.energy_kwh, 6),
            "charging_kwh": round(self.charging_kwh, 6),
            "lateness_minutes": round(self.lateness_minutes, 6),
            "on_time": self.on_time,
            "vehicles_used": self.vehicles_used,
            "served_customers": list(self.served_customers),
            "missing_customers": list(self.missing_customers),
            "duplicate_customers": list(self.duplicate_customers),
            "violations": list(self.violations),
            "feasible": self.feasible,
            "route_details": list(self.route_details),
        }


@dataclass(frozen=True, slots=True)
class SolveResult:
    method: str
    seed: int
    iterations: int
    ants: int
    plan: RoutePlan
    evaluation: Evaluation
    baseline_plan: RoutePlan
    baseline_evaluation: Evaluation
    history: tuple[float, ...]
    # A cancelled search is still a useful diagnostic artifact, but it must
    # never be rendered as a completed ACO result.
    complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "method": self.method,
            "seed": self.seed,
            "iterations": self.iterations,
            "ants": self.ants,
            "plan": self.plan.as_dict(),
            "evaluation": self.evaluation.as_dict(),
            "baseline_plan": self.baseline_plan.as_dict(),
            "baseline_evaluation": self.baseline_evaluation.as_dict(),
            "history": list(self.history),
            "complete": self.complete,
            "completed_iterations": len(self.history),
        }
