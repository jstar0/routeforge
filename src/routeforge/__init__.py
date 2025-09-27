"""RouteForge: auditable, state-aware electric routing experiments."""

from .benchmark import ablation_payload, benchmark_payload, write_ablation, write_benchmark
from .evaluation import dynamic_charge_amount, evaluate_plan
from .instance import (
    battery_bucket,
    distance,
    energy_for,
    generate_instance,
    instance_from_dict,
    load_instance_json,
    travel_minutes,
)
from .models import CostModel, Evaluation, Node, ProblemInstance, RoutePlan, SolveResult, Vehicle
from .solver import SolverConfig, StateAwareACO, solve
from .sweep import parse_seed_list, seed_sweep_payload, write_sweep

__version__ = "0.1.0"

__all__ = [
    "CostModel",
    "Evaluation",
    "Node",
    "ProblemInstance",
    "RoutePlan",
    "SolveResult",
    "SolverConfig",
    "StateAwareACO",
    "Vehicle",
    "__version__",
    "ablation_payload",
    "battery_bucket",
    "benchmark_payload",
    "distance",
    "dynamic_charge_amount",
    "energy_for",
    "evaluate_plan",
    "generate_instance",
    "instance_from_dict",
    "load_instance_json",
    "parse_seed_list",
    "seed_sweep_payload",
    "solve",
    "travel_minutes",
    "write_ablation",
    "write_benchmark",
    "write_sweep",
]
