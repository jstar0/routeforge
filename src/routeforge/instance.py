from __future__ import annotations

import json
import math
import random
from pathlib import Path
from typing import Any

from .models import CostModel, Node, ProblemInstance, Vehicle


def distance(a: Node, b: Node) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def travel_minutes(km: float, vehicle: Vehicle) -> float:
    return km / vehicle.speed_kmph * 60.0


def energy_for(km: float, load_kg: float, vehicle: Vehicle) -> float:
    return km * (
        vehicle.base_consumption_kwh_per_km + vehicle.load_consumption_kwh_per_kg_km * load_kg
    )


def battery_bucket(battery_kwh: float, vehicle: Vehicle, levels: int = 4) -> int:
    fraction = max(0.0, min(1.0, battery_kwh / vehicle.battery_capacity_kwh))
    return min(levels - 1, int(fraction * levels))


def generate_instance(
    *,
    customers: int = 12,
    chargers: int = 3,
    vehicles: int | None = None,
    scale_km: float = 40.0,
    seed: int = 42,
) -> ProblemInstance:
    """Create a deterministic, moderate-size instance suitable for demos and tests."""

    if customers < 1 or chargers < 0 or scale_km <= 0:
        raise ValueError("customers must be positive, chargers non-negative, and scale positive")
    rng = random.Random(seed)
    center = scale_km / 2.0
    nodes: list[Node] = [Node(0, center, center, "depot")]
    for node_id in range(1, customers + 1):
        angle = rng.random() * math.tau
        radius = scale_km * (0.18 + rng.random() * 0.30)
        x = max(0.5, min(scale_km - 0.5, center + math.cos(angle) * radius))
        y = max(0.5, min(scale_km - 0.5, center + math.sin(angle) * radius))
        earliest = 45.0 + rng.random() * 180.0
        nodes.append(
            Node(
                node_id,
                x,
                y,
                "customer",
                dry_demand=40.0 + rng.random() * 100.0,
                recyclable_demand=30.0 + rng.random() * 80.0,
                earliest_minute=earliest,
                latest_minute=earliest + 300.0 + rng.random() * 100.0,
                service_minutes=6.0 + rng.random() * 10.0,
            )
        )
    for offset in range(chargers):
        angle = math.tau * offset / max(1, chargers) + 0.15
        radius = scale_km * 0.30
        nodes.append(
            Node(
                customers + 1 + offset,
                center + math.cos(angle) * radius,
                center + math.sin(angle) * radius,
                "charger",
            )
        )
    vehicle_count = vehicles if vehicles is not None else max(2, math.ceil(customers / 6))
    fleet = tuple(Vehicle(index) for index in range(vehicle_count))
    return ProblemInstance(tuple(nodes), fleet)


def instance_from_dict(payload: Any) -> ProblemInstance:
    """Parse the versioned, portable scene format used by the workbench.

    This is deliberately strict: a browser/API must not be able to smuggle
    NaN, Infinity, duplicate IDs, or a partial fleet into the evaluator.
    """
    if not isinstance(payload, dict):
        raise ValueError("scene must be a JSON object")
    if payload.get("schema_version", 1) != 1:
        raise ValueError("scene schema_version must be 1")
    if payload.get("coordinate_unit", "km") != "km":
        raise ValueError("coordinate_unit must be km")
    raw_nodes = payload.get("nodes")
    raw_vehicles = payload.get("vehicles")
    raw_costs = payload.get("costs", {})
    if not isinstance(raw_nodes, list) or not isinstance(raw_vehicles, list) or not isinstance(raw_costs, dict):
        raise ValueError("scene requires nodes, vehicles, and costs objects")
    if len(raw_nodes) > 231 or len(raw_vehicles) > 20:
        raise ValueError("scene exceeds the node or vehicle limit")
    try:
        nodes = tuple(Node(**row) for row in raw_nodes)
        vehicles = tuple(Vehicle(**row) for row in raw_vehicles)
        costs = CostModel(**raw_costs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid scene field: {exc}") from exc
    if len([node for node in nodes if node.kind == "customer"]) > 200:
        raise ValueError("scene supports at most 200 customers")
    if len([node for node in nodes if node.kind == "charger"]) > 30:
        raise ValueError("scene supports at most 30 chargers")
    if any(not (0.0 <= node.x <= 200.0 and 0.0 <= node.y <= 200.0) for node in nodes):
        raise ValueError("node coordinates must be within 0..200 km")
    if any(not (0.0 <= node.earliest_minute <= 1440.0 and 0.0 <= node.latest_minute <= 1440.0) for node in nodes):
        raise ValueError("time windows must be within 0..1440 minutes")
    return ProblemInstance(nodes, vehicles, costs)


def load_instance_json(path: Path) -> ProblemInstance:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read scene JSON: {exc}") from exc
    return instance_from_dict(payload)


def instance_summary(instance: ProblemInstance) -> dict[str, Any]:
    return {
        "nodes": len(instance.nodes),
        "customers": len(instance.customers),
        "chargers": len(instance.chargers),
        "vehicles": len(instance.vehicles),
        "scale_hint_km": round(max(distance(instance.depot, node) for node in instance.nodes), 3),
    }
