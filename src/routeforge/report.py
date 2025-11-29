from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .models import ProblemInstance, SolveResult


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _route_map(instance: ProblemInstance, result: SolveResult) -> str:
    width, height, pad = 760, 460, 38
    xs = [node.x for node in instance.nodes]
    ys = [node.y for node in instance.nodes]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    def point(node_id: int) -> tuple[float, float]:
        node = instance.node_by_id[node_id]
        x = pad + (node.x - min_x) / max(max_x - min_x, 1e-9) * (width - 2 * pad)
        y = height - pad - (node.y - min_y) / max(max_y - min_y, 1e-9) * (height - 2 * pad)
        return x, y

    colors = ("#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2")
    route_lines: list[str] = []
    for index, route in enumerate(result.plan.routes):
        points = " ".join(f"{point(node_id)[0]:.1f},{point(node_id)[1]:.1f}" for node_id in route if node_id in instance.node_by_id)
        route_lines.append(
            f'<polyline points="{points}" fill="none" stroke="{colors[index % len(colors)]}" '
            'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity=".78"/>'
        )
    nodes_svg: list[str] = []
    for node in instance.nodes:
        x, y = point(node.id)
        if node.kind == "depot":
            shape = f'<rect x="{x-7:.1f}" y="{y-7:.1f}" width="14" height="14" rx="3" fill="#111827"/>'
        elif node.kind == "charger":
            shape = f'<path d="M {x:.1f} {y-9:.1f} L {x+8:.1f} {y+7:.1f} L {x-8:.1f} {y+7:.1f} Z" fill="#f59e0b"/>'
        else:
            shape = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="#ef4444"/>'
        nodes_svg.append(f'{shape}<text x="{x+9:.1f}" y="{y+4:.1f}" font-size="11" fill="#475569">{node.id}</text>')
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Route map" '
        'xmlns="http://www.w3.org/2000/svg" style="background:#f8fafc;border-radius:12px">'
        + "".join(route_lines)
        + "".join(nodes_svg)
        + "</svg>"
    )


def _convergence_svg(result: SolveResult) -> str:
    if not result.history:
        return "<p>No iterations recorded.</p>"
    width, height, pad = 760, 220, 34
    low, high = min(result.history), max(result.history)
    spread = max(high - low, 1e-9)
    points = []
    for index, value in enumerate(result.history):
        x = pad + index / max(1, len(result.history) - 1) * (width - 2 * pad)
        y = height - pad - (value - low) / spread * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")
    path = " ".join(points)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Cost convergence" '
        'xmlns="http://www.w3.org/2000/svg">'
        f'<line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" stroke="#cbd5e1"/>'
        f'<polyline points="{path}" fill="none" stroke="#2563eb" stroke-width="3" stroke-linejoin="round"/>'
        f'<text x="{pad}" y="18" fill="#475569" font-size="12">best total cost</text>'
        "</svg>"
    )


def write_report(result: SolveResult, instance: ProblemInstance, output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_text(output_dir / "instance.json", json.dumps(instance.as_dict(), ensure_ascii=False, indent=2) + "\n")
    _write_text(output_dir / "result.json", json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n")
    routes_csv = output_dir / "routes.csv"
    with routes_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "vehicle_id",
                "vehicle_index",
                "route",
                "customers",
                "distance_km",
                "energy_kwh",
                "charging_kwh",
                "lateness_minutes",
                "finish_minute",
                "finish_battery_kwh",
                "violations",
            ),
        )
        writer.writeheader()
        for detail in result.evaluation.route_details:
            writer.writerow(
                {
                    key: detail[key]
                    for key in writer.fieldnames
                    if key not in {"route", "customers", "violations"}
                }
                | {
                    "route": json.dumps(detail["route"]),
                    "customers": json.dumps(detail["customers"]),
                    "violations": json.dumps(detail["violations"]),
                }
            )
    stops_csv = output_dir / "stops.csv"
    with stops_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = (
            "vehicle_id", "vehicle_index", "node_id", "sequence", "kind",
            "arrival_minute", "service_start_minute", "departure_minute",
            "waiting_minutes", "charge_minutes", "battery_before_kwh",
            "battery_after_kwh", "dry_load_kg", "recyclable_load_kg", "violations",
        )
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for detail in result.evaluation.route_details:
            for stop in detail["stops"]:
                writer.writerow(
                    {"vehicle_id": detail["vehicle_id"], "vehicle_index": detail["vehicle_index"], **stop,
                     "violations": json.dumps(stop["violations"])}
                )

    evaluation = result.evaluation
    baseline = result.baseline_evaluation
    violation_list = "".join(f"<li>{html.escape(item)}</li>" for item in evaluation.violations) or "<li>None</li>"
    missing = ", ".join(map(str, evaluation.missing_customers)) or "None"
    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RouteForge · {html.escape(result.method)}</title>
<style>
:root {{ font-family: Inter, ui-sans-serif, system-ui, sans-serif; color:#172033; color-scheme:light; }}
body {{ margin:0; background:#f5f7fb; }} main {{ max-width:1120px; margin:auto; padding:38px 22px 64px; }}
h1 {{ margin:0 0 6px; letter-spacing:-.03em; }} h2 {{ margin:32px 0 12px; }} .lede,.meta {{ color:#526071; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(165px,1fr)); gap:12px; margin:24px 0; }}
.card,.panel {{ background:#fff; border:1px solid #dfe6f1; border-radius:14px; padding:16px; }}
.label {{ color:#667085; font-size:.8rem; }} .value {{ font-size:1.65rem; font-weight:750; margin-top:4px; }}
.ok {{ color:#15803d; }} .warn {{ color:#b45309; }} table {{ width:100%; border-collapse:collapse; }} th,td {{ text-align:left; padding:9px 10px; border-bottom:1px solid #edf1f6; vertical-align:top; }} th {{ background:#f1f5fa; }} code {{ white-space:pre-wrap; word-break:break-word; }}
.note {{ background:#fff8e7; border:1px solid #f3d58f; border-radius:12px; padding:13px 15px; }} footer {{ color:#7b8494; font-size:.8rem; margin-top:36px; }}
</style></head><body><main>
<h1>RouteForge routing report</h1>
<p class="lede">A state-aware electric multi-compartment route plan with explicit battery, capacity, and time-window accounting.</p>
<p class="meta">method <code>{html.escape(result.method)}</code> · seed {result.seed} · {result.iterations} iterations · {result.ants} ants</p>
<section class="cards">
<div class="card"><div class="label">Plan cost</div><div class="value">{evaluation.total_cost:.2f}</div></div>
<div class="card"><div class="label">Distance</div><div class="value">{evaluation.distance_km:.1f} km</div></div>
<div class="card"><div class="label">Charging</div><div class="value">{evaluation.charging_kwh:.1f} kWh</div></div>
<div class="card"><div class="label">Lateness</div><div class="value">{evaluation.lateness_minutes:.1f} min</div></div>
<div class="card"><div class="label">Feasibility</div><div class="value {'ok' if evaluation.feasible else 'warn'}">{'PASS' if evaluation.feasible else 'CHECK'}</div></div>
</section>
<h2>Routes</h2><div class="panel">{_route_map(instance, result)}</div>
<h2>Convergence</h2><div class="panel">{_convergence_svg(result)}</div>
<h2>Baseline comparison</h2><div class="panel"><table><thead><tr><th>Plan</th><th>Total cost</th><th>Distance</th><th>Charging</th><th>Late</th><th>Feasible</th></tr></thead><tbody>
<tr><td>Greedy baseline</td><td>{baseline.total_cost:.2f}</td><td>{baseline.distance_km:.1f} km</td><td>{baseline.charging_kwh:.1f} kWh</td><td>{baseline.lateness_minutes:.1f} min</td><td>{'yes' if baseline.feasible else 'no'}</td></tr>
<tr><td>State-aware ACO</td><td>{evaluation.total_cost:.2f}</td><td>{evaluation.distance_km:.1f} km</td><td>{evaluation.charging_kwh:.1f} kWh</td><td>{evaluation.lateness_minutes:.1f} min</td><td>{'yes' if evaluation.feasible else 'no'}</td></tr>
</tbody></table></div>
<h2>Diagnostics</h2><div class="panel"><p>Served {len(evaluation.served_customers)}/{len(instance.customers)} customers · missing: <code>{html.escape(missing)}</code></p><ul>{violation_list}</ul></div>
<h2>Route details</h2><div class="panel"><table><thead><tr><th>Vehicle</th><th>Customers</th><th>Distance</th><th>Energy</th><th>Charge</th><th>Finish battery</th></tr></thead><tbody>
{"".join(f"<tr><td>{d['vehicle_id']}</td><td>{html.escape(str(d['customers']))}</td><td>{d['distance_km']:.1f} km</td><td>{d['energy_kwh']:.1f} kWh</td><td>{d['charging_kwh']:.1f} kWh</td><td>{d['finish_battery_kwh']:.1f} kWh</td></tr>" for d in evaluation.route_details)}
</tbody></table></div>
<p class="note">The score is instance-specific. Reuse the saved instance, seed, cost model, and package version before comparing runs.</p>
<footer>Generated by RouteForge · independent implementation; no client code or data included.</footer>
</main></body></html>"""
    html_path = output_dir / "report.html"
    _write_text(html_path, document)
    return {
        "instance": str(output_dir / "instance.json"),
        "result": str(output_dir / "result.json"),
        "routes": str(routes_csv),
        "stops": str(stops_csv),
        "html": str(html_path),
    }
