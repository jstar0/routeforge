from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmark import write_ablation, write_benchmark
from .instance import generate_instance, instance_summary, load_instance_json
from .report import write_report
from .solver import SolverConfig, solve
from .sweep import parse_seed_list, write_sweep


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="routeforge",
        description="Solve auditable electric multi-compartment routing instances.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    solve_parser = subparsers.add_parser("solve", help="Generate or solve one deterministic instance")
    solve_parser.add_argument("--instance", type=Path, help="Versioned scene JSON to solve instead of generating")
    solve_parser.add_argument("--customers", type=_positive, default=12)
    solve_parser.add_argument("--chargers", type=int, default=3)
    solve_parser.add_argument("--vehicles", type=_positive, default=None)
    solve_parser.add_argument("--scale-km", type=float, default=40.0)
    solve_parser.add_argument("--seed", type=int, default=42)
    solve_parser.add_argument("--ants", type=_positive, default=18)
    solve_parser.add_argument("--iterations", type=_positive, default=35)
    solve_parser.add_argument("--output-dir", type=Path, default=Path("outputs/solve"))

    demo_parser = subparsers.add_parser("demo", help="Run a compact instance and generate a report")
    demo_parser.add_argument("--seed", type=int, default=42)
    demo_parser.add_argument("--output-dir", type=Path, default=Path("outputs/demo"))
    benchmark_parser = subparsers.add_parser("benchmark", help="Compare ACO with the greedy baseline")
    benchmark_parser.add_argument("--seed", type=int, default=17)
    benchmark_parser.add_argument("--output-dir", type=Path, default=Path("outputs/benchmark"))
    ablation_parser = subparsers.add_parser(
        "ablation", help="Test whether the battery-state pheromone dimension earns its place"
    )
    ablation_parser.add_argument("--seeds", default="17,29,41,53,67,71,83,97")
    ablation_parser.add_argument("--output-dir", type=Path, default=Path("outputs/ablation"))
    sweep_parser = subparsers.add_parser("sweep", help="Repeat the benchmark over explicit seeds")
    sweep_parser.add_argument("--seeds", default="17,29,41")
    sweep_parser.add_argument("--output-dir", type=Path, default=Path("outputs/sweep"))
    serve_parser = subparsers.add_parser("serve", help="Serve the local RouteForge API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=_positive, default=5203)
    serve_parser.add_argument("--runtime-dir", type=Path, default=Path("runtime/routeforge"))
    return parser


def _run(args: argparse.Namespace) -> int:
    source_path = getattr(args, "instance", None)
    instance = load_instance_json(source_path) if source_path else generate_instance(
        customers=args.customers, chargers=args.chargers, vehicles=args.vehicles,
        scale_km=args.scale_km, seed=args.seed,
    )
    result = solve(
        instance,
        seed=args.seed,
        config=SolverConfig(ants=args.ants, iterations=args.iterations),
    )
    artifacts = write_report(result, instance, args.output_dir)
    payload = {
        "instance": instance_summary(instance),
        "method": result.method,
        "seed": result.seed,
        "iterations": result.iterations,
        "ants": result.ants,
        "cost": result.evaluation.total_cost,
        "feasible": result.evaluation.feasible,
        "served_customers": len(result.evaluation.served_customers),
        "total_customers": len(instance.customers),
        "baseline_cost": result.baseline_evaluation.total_cost,
        "artifacts": artifacts,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "benchmark":
        payload = write_benchmark(args.output_dir, args.seed)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "ablation":
        payload = write_ablation(args.output_dir, tuple(parse_seed_list(args.seeds)))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "sweep":
        payload = write_sweep(args.output_dir, parse_seed_list(args.seeds))
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "demo":
        args.customers = 12
        args.chargers = 3
        args.vehicles = 3
        args.scale_km = 40.0
        args.ants = 12
        args.iterations = 24
        return _run(args)
    if args.command == "serve":
        import uvicorn

        from .app.main import create_app
        uvicorn.run(create_app(args.runtime_dir), host=args.host, port=args.port)
        return 0
    return _run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
