"""Record real API responses for the static GitHub Pages replay.

Starts the real backend on a temporary runtime directory, creates scenarios
through the API, solves them with the same parameters the workbench uses
(aco, 100 iterations, 20 ants, seed 42) and stores the responses verbatim in
web/src/replay/recording.json.

    uv run python scripts/record_replay.py
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from routeforge.instance import generate_instance

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "web" / "src" / "replay" / "recording.json"
SOLVE = {"algorithm": "aco", "iterations": 100, "ants": 20, "seed": 42}

# Same scene as `example` in web/src/main.tsx (the workbench's initial draft).
BUILTIN = {
    "schema_version": 1,
    "coordinate_unit": "km",
    "nodes": [
        {"id": 0, "kind": "depot", "x": 12, "y": 12, "dry_demand": 0, "recyclable_demand": 0, "earliest_minute": 0, "latest_minute": 480, "service_minutes": 0},
        {"id": 1, "kind": "customer", "x": 28, "y": 24, "dry_demand": 120, "recyclable_demand": 65, "earliest_minute": 30, "latest_minute": 180, "service_minutes": 8},
        {"id": 2, "kind": "customer", "x": 54, "y": 18, "dry_demand": 80, "recyclable_demand": 110, "earliest_minute": 60, "latest_minute": 260, "service_minutes": 10},
        {"id": 3, "kind": "customer", "x": 62, "y": 51, "dry_demand": 180, "recyclable_demand": 45, "earliest_minute": 100, "latest_minute": 360, "service_minutes": 12},
        {"id": 4, "kind": "charger", "x": 40, "y": 43, "dry_demand": 0, "recyclable_demand": 0, "earliest_minute": 0, "latest_minute": 1440, "service_minutes": 0},
    ],
    "vehicles": [
        {"id": 1, "battery_capacity_kwh": 120, "dry_capacity_kg": 1200, "recyclable_capacity_kg": 1000, "speed_kmph": 40, "charging_power_kw": 90, "base_consumption_kwh_per_km": 0.75, "load_consumption_kwh_per_kg_km": 0.0002, "safety_battery_fraction": 0.2, "fixed_cost": 280}
    ],
    "costs": {"distance_cost_per_km": 1.2, "energy_cost_per_kwh": 0.8, "lateness_cost_per_minute": 8, "missed_customer_penalty": 10000},
}


def scenes() -> list[tuple[str, dict[str, Any]]]:
    demo = generate_instance(customers=12, chargers=3, vehicles=3, scale_km=40.0, seed=17)
    out = [
        ("工作台内置示例（3 个收集点 + 1 座充电站）", BUILTIN),
        ("合成场景：12 个收集点 / 3 台车（demo seed 17）", {"schema_version": 1, "coordinate_unit": "km", **demo.as_dict()}),
    ]
    labels = {
        "normal-feasible": "固定案例：普通可行（2 个收集点）",
        "requires-charging": "固定案例：需要中途充电",
        "tight-search-infeasible": "固定案例：时间窗过紧，搜索未找到可行方案",
    }
    for name, label in labels.items():
        out.append((label, json.loads((ROOT / "examples" / "cases" / f"{name}.json").read_text())))
    return out


def call(base: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def sse(base: str, path: str) -> list[dict[str, Any]]:
    """Read the finished job's full event stream and parse it as the browser would."""
    with urllib.request.urlopen(base + path, timeout=120) as resp:
        text = resp.read().decode()
    events = []
    for block in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            events.append({"type": fields["event"], "id": fields["id"], "data": fields["data"]})
    return events


def main() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory() as runtime:
        server = subprocess.Popen(
            [sys.executable, "-m", "routeforge.cli", "serve", "--host", "127.0.0.1", "--port", str(port), "--runtime-dir", runtime],
            cwd=ROOT,
        )
        try:
            for _ in range(100):
                try:
                    call(base, "GET", "/api/v1/health")
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("backend did not start")

            entries: list[dict[str, Any]] = []
            # Created in reverse so the newest-first list shows the built-in example first.
            for label, scene in reversed(scenes()):
                status, created = call(base, "POST", "/api/v1/scenarios", {"scene": scene})
                assert status == 201, created
                sid, rev = created["scenario_id"], created["revision"]
                status, fetched = call(base, "GET", f"/api/v1/scenarios/{sid}/{rev}")
                assert status == 200
                status, validated = call(base, "POST", "/api/v1/scenarios/validate", {"scene": scene})
                assert status == 200, validated
                status, solve_created = call(base, "POST", "/api/v1/solve-runs", {"scenario_id": sid, "revision": rev, **SOLVE})
                assert status == 202, solve_created
                job_id = solve_created["job_id"]
                while True:
                    _, job = call(base, "GET", f"/api/v1/solve-runs/{job_id}")
                    if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                        break
                    time.sleep(0.2)
                assert job["status"] == "succeeded", job
                events = sse(base, f"/api/v1/solve-runs/{job_id}/events?after=0")
                _, plans = call(base, "GET", f"/api/v1/solve-runs/{job_id}/plans")
                plan_id = plans["plans"][0]["plan_id"]
                _, plan = call(base, "GET", f"/api/v1/plans/{plan_id}")
                print(f"{label}: {len(events)} events, feasible={plan['result']['evaluation']['feasible']}, cost={plan['result']['evaluation']['total_cost']:.2f}")
                entries.append({
                    "label": label,
                    "request_scene": scene,
                    "create": {"scenario_id": sid, "revision": rev, **{k: v for k, v in created.items() if k not in {"scenario_id", "revision"}}},
                    "validate": validated,
                    "scenario": fetched,
                    "solve_request": {"scenario_id": sid, "revision": rev, **SOLVE},
                    "solve_created": solve_created,
                    "job": job,
                    "events": events,
                    "plans": plans,
                    "plan": plan,
                })
            entries.reverse()
            _, listing = call(base, "GET", "/api/v1/scenarios?limit=200")
            recording = {"solve": SOLVE, "list": listing, "labels": {e["create"]["scenario_id"]: e["label"] for e in entries}, "entries": entries}
        finally:
            server.terminate()
            server.wait(timeout=15)
    OUTPUT.write_text(json.dumps(recording, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({OUTPUT.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
