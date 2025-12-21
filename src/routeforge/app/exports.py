"""Exports built only from persisted RouteForge plans and canonical stops."""

from __future__ import annotations

import csv
import html
import io
import json
from typing import Any

from ..models import ProblemInstance


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return f"'{value}"
    return value


def stops_csv(payload: dict[str, Any]) -> bytes:
    stream = io.StringIO(newline="")
    fields = (
        "vehicle_id", "node_id", "sequence", "kind", "arrival_minute", "service_start_minute",
        "departure_minute", "waiting_minutes", "charge_minutes", "battery_before_kwh",
        "battery_after_kwh", "dry_load_kg", "recyclable_load_kg", "violations",
    )
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for detail in payload["evaluation"]["route_details"]:
        for stop in detail["stops"]:
            writer.writerow(
                {
                    "vehicle_id": detail["vehicle_id"],
                    **{key: _csv_cell(stop[key]) for key in fields if key in stop},
                    "violations": _csv_cell(json.dumps(stop["violations"], ensure_ascii=False)),
                }
            )
    return stream.getvalue().encode("utf-8-sig")


def route_svg(instance: ProblemInstance, payload: dict[str, Any]) -> bytes:
    width, height, pad = 760, 460, 38
    xs = [node.x for node in instance.nodes]
    ys = [node.y for node in instance.nodes]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    def point(node_id: int) -> tuple[float, float]:
        node = instance.node_by_id[node_id]
        return (
            pad + (node.x - min_x) / max(max_x - min_x, 1e-9) * (width - 2 * pad),
            height - pad - (node.y - min_y) / max(max_y - min_y, 1e-9) * (height - 2 * pad),
        )

    colors = ("#e07155", "#3e73a3", "#4b8d65", "#9d6cae", "#be8d3b")
    lines = []
    for index, route in enumerate(payload["plan"]["routes"]):
        points = " ".join(
            f"{point(node)[0]:.1f},{point(node)[1]:.1f}"
            for node in route
            if node in instance.node_by_id
        )
        lines.append(
            f'<polyline points="{points}" fill="none" stroke="{colors[index % len(colors)]}" '
            'stroke-width="3" opacity=".82"/>'
        )
    shapes = []
    for node in instance.nodes:
        x, y = point(node.id)
        if node.kind == "depot":
            shape = f'<rect x="{x-6:.1f}" y="{y-6:.1f}" width="12" height="12" rx="2" fill="#24322b"/>'
        elif node.kind == "charger":
            shape = f'<path d="M{x:.1f} {y-8:.1f}L{x+7:.1f} {y+6:.1f}L{x-7:.1f} {y+6:.1f}Z" fill="#db855b"/>'
        else:
            shape = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#3e73a3"/>'
        shapes.append(f'{shape}<text x="{x+7:.1f}" y="{y+3:.1f}" font-size="11" fill="#24322b">{node.id}</text>')
    source = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
        'aria-label="RouteForge route plan" style="background:#f7f7f2">'
        + "".join(lines)
        + "".join(shapes)
        + "</svg>"
    )
    return source.encode()


def report_html(instance: ProblemInstance, payload: dict[str, Any], plan_id: str) -> bytes:
    evaluation = payload["evaluation"]
    details = "".join(
        f"<tr><td>{html.escape(str(row['vehicle_id']))}</td><td>{row['distance_km']:.1f}</td>"
        f"<td>{row['energy_kwh']:.1f}</td><td>{row['lateness_minutes']:.1f}</td>"
        f"<td>{html.escape(', '.join(row['violations']) or '—')}</td></tr>"
        for row in evaluation["route_details"]
    )
    violations = "".join(f"<li>{html.escape(item)}</li>" for item in evaluation["violations"]) or "<li>none</li>"
    cards = "".join(
        f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div></div>'
        for label, value in (
            ("Cost", f"{evaluation['total_cost']:.2f}"),
            ("Distance", f"{evaluation['distance_km']:.1f} km"),
            ("Energy", f"{evaluation['energy_kwh']:.1f} kWh"),
            ("Feasible", str(evaluation["feasible"])),
        )
    )
    document = f"""<!doctype html>
<meta charset="utf-8">
<title>RouteForge plan {html.escape(plan_id)}</title>
<style>
body{{font:14px system-ui,sans-serif;background:#f7f7f2;color:#24322b;margin:32px;max-width:960px}}
table{{border-collapse:collapse;width:100%}}
th,td{{text-align:left;padding:8px;border-bottom:1px solid #dce1d8}}
.cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{background:white;border:1px solid #dce1d8;border-radius:8px;padding:12px;min-width:120px}}
.label{{color:#68756b;font-size:12px}}.value{{font-size:20px;font-weight:700;margin-top:4px}}
</style>
<h1>RouteForge plan report</h1>
<p>Plan <code>{html.escape(plan_id)}</code> · method <code>{html.escape(payload['method'])}</code>
· complete {payload['complete']}</p>
<div class="cards">{cards}</div>
<h2>Routes</h2>{route_svg(instance, payload).decode()}
<h2>Vehicle details</h2>
<table><thead><tr><th>Vehicle</th><th>Distance km</th><th>Energy kWh</th>
<th>Late min</th><th>Violations</th></tr></thead><tbody>{details}</tbody></table>
<h2>Diagnostics</h2><ul>{violations}</ul>"""
    return document.encode("utf-8")
