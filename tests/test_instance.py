import math

import pytest

from routeforge import CostModel, Node, ProblemInstance, Vehicle
from routeforge.instance import battery_bucket, distance, energy_for, generate_instance, instance_from_dict


def test_instance_generation_is_seeded() -> None:
    first = generate_instance(customers=5, chargers=2, seed=11)
    second = generate_instance(customers=5, chargers=2, seed=11)
    assert first.as_dict() == second.as_dict()
    assert len(first.customers) == 5
    assert len(first.chargers) == 2


def test_energy_and_battery_bucket_are_monotonic() -> None:
    instance = generate_instance(customers=2, chargers=1, seed=2)
    vehicle = instance.vehicles[0]
    km = distance(instance.depot, instance.customers[0])
    assert energy_for(km, 100.0, vehicle) > energy_for(km, 0.0, vehicle)
    assert battery_bucket(vehicle.battery_capacity_kwh, vehicle) == 3
    assert battery_bucket(0.0, vehicle) == 0


def test_scene_json_rejects_nonfinite_values_and_duplicate_vehicle_ids() -> None:
    payload = generate_instance(customers=2, chargers=1, seed=4).as_dict()
    payload["schema_version"] = 1
    payload["coordinate_unit"] = "km"
    payload["nodes"][1]["x"] = math.nan
    with pytest.raises(ValueError, match="finite"):
        instance_from_dict(payload)
    with pytest.raises(ValueError, match="vehicle IDs"):
        ProblemInstance((Node(0, 0, 0, "depot"),), (Vehicle(1), Vehicle(1)), CostModel())
