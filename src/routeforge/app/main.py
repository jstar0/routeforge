from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..evaluation import evaluate_plan
from ..instance import instance_from_dict
from ..models import RoutePlan
from .exports import report_html, route_svg, stops_csv
from .jobs import JobNotFoundError, QueueFullError, RouteJobManager


class SceneRequest(BaseModel):
    scene: Any


class SolveRequest(BaseModel):
    scenario_id: str
    revision: int
    algorithm: str = "aco"
    seed: int = 42
    iterations: int = 100
    ants: int = 20


class ManualPlanRequest(BaseModel):
    scenario_id: str
    revision: int
    routes: list[list[int]]
    vehicle_ids: list[int]


class ComparisonRequest(BaseModel):
    plan_ids: list[str]


class ExportRequest(BaseModel):
    formats: list[str]


def _scene_field_errors(payload: Any) -> list[dict[str, str]]:
    """Return concrete JSON paths before the domain parser raises a batch error."""

    errors: list[dict[str, str]] = []

    def add(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    if not isinstance(payload, dict):
        return [{"path": "scene", "message": "必须是 JSON 对象"}]
    if type(payload.get("schema_version", 1)) is not int or payload.get("schema_version", 1) != 1:
        add("schema_version", "只支持版本 1")
    if payload.get("coordinate_unit", "km") != "km":
        add("coordinate_unit", "坐标单位必须为 km")
    for key in sorted(set(payload) - {"schema_version", "coordinate_unit", "nodes", "vehicles", "costs"}):
        add(key, "未知字段")

    raw_nodes = payload.get("nodes")
    raw_vehicles = payload.get("vehicles")
    raw_costs = payload.get("costs", {})
    if not isinstance(raw_nodes, list):
        add("nodes", "必须是点位数组")
        raw_nodes = []
    if not isinstance(raw_vehicles, list):
        add("vehicles", "必须是车辆数组")
        raw_vehicles = []
    if not isinstance(raw_costs, dict):
        add("costs", "必须是对象")
        raw_costs = {}

    if not raw_nodes:
        add("nodes", "至少需要一个点位和一个仓库")
    if len(raw_nodes) > 231:
        add("nodes", "点位总数不能超过 231")
    if not raw_vehicles:
        add("vehicles", "至少需要一辆车")
    if len(raw_vehicles) > 20:
        add("vehicles", "车辆数不能超过 20")

    allowed_node_fields = {
        "id", "x", "y", "kind", "dry_demand", "recyclable_demand",
        "earliest_minute", "latest_minute", "service_minutes",
    }
    node_defaults = {
        "dry_demand": 0.0, "recyclable_demand": 0.0, "earliest_minute": 0.0,
        "latest_minute": 1440.0, "service_minutes": 0.0,
    }
    numeric_node_fields = {
        "x": (0.0, 200.0), "y": (0.0, 200.0),
        "dry_demand": (0.0, None), "recyclable_demand": (0.0, None),
        "earliest_minute": (0.0, 1440.0), "latest_minute": (0.0, 1440.0),
        "service_minutes": (0.0, None),
    }
    node_ids: set[int] = set()
    depot_count = 0
    customer_count = 0
    charger_count = 0
    for index, row in enumerate(raw_nodes):
        prefix = f"nodes[{index}]"
        if not isinstance(row, dict):
            add(prefix, "必须是对象")
            continue
        for key in sorted(set(row) - allowed_node_fields):
            add(f"{prefix}.{key}", "未知字段")
        for key in ("id", "kind"):
            if key not in row:
                add(f"{prefix}.{key}", "必填")
        node_id = row.get("id")
        if type(node_id) is not int or node_id < 0:
            add(f"{prefix}.id", "必须是非负整数")
        elif node_id in node_ids:
            add(f"{prefix}.id", "点位 ID 重复")
        else:
            node_ids.add(node_id)
        kind = row.get("kind")
        if not isinstance(kind, str) or kind not in {"depot", "customer", "charger"}:
            add(f"{prefix}.kind", "必须是 depot、customer 或 charger")
        elif kind == "depot":
            depot_count += 1
        elif kind == "customer":
            customer_count += 1
        else:
            charger_count += 1
        numeric_values: dict[str, float] = {}
        for key, (minimum, maximum) in numeric_node_fields.items():
            if key not in row and key not in node_defaults:
                add(f"{prefix}.{key}", "必填")
                continue
            value = row.get(key, node_defaults.get(key))
            number = _finite_number(value)
            if number is None:
                add(f"{prefix}.{key}", "必须是有限数值")
                continue
            numeric_values[key] = number
            if minimum is not None and number < minimum:
                add(f"{prefix}.{key}", f"不能小于 {minimum:g}")
            if maximum is not None and number > maximum:
                add(f"{prefix}.{key}", f"不能大于 {maximum:g}")
        if isinstance(kind, str) and kind in {"depot", "charger"} and (
            numeric_values.get("dry_demand", 0.0) > 0
            or numeric_values.get("recyclable_demand", 0.0) > 0
        ):
            add(prefix, "只有 customer 可以填写需求")
        if "earliest_minute" in numeric_values and "latest_minute" in numeric_values and numeric_values["earliest_minute"] > numeric_values["latest_minute"]:
            add(f"{prefix}.latest_minute", "必须不早于 earliest_minute")
    if depot_count != 1:
        add("nodes", "必须且只能有一个 depot")
    if customer_count > 200:
        add("nodes", "customer 数不能超过 200")
    if charger_count > 30:
        add("nodes", "charger 数不能超过 30")

    allowed_vehicle_fields = {
        "id", "battery_capacity_kwh", "dry_capacity_kg", "recyclable_capacity_kg",
        "base_consumption_kwh_per_km", "load_consumption_kwh_per_kg_km", "speed_kmph",
        "safety_battery_fraction", "charging_power_kw", "fixed_cost",
    }
    vehicle_defaults = {
        "battery_capacity_kwh": 120.0, "dry_capacity_kg": 1200.0,
        "recyclable_capacity_kg": 1000.0, "base_consumption_kwh_per_km": 0.75,
        "load_consumption_kwh_per_kg_km": 0.0002, "speed_kmph": 40.0,
        "safety_battery_fraction": 0.2, "charging_power_kw": 90.0, "fixed_cost": 280.0,
    }
    positive_vehicle_fields = {
        "battery_capacity_kwh", "dry_capacity_kg", "recyclable_capacity_kg",
        "base_consumption_kwh_per_km", "speed_kmph", "charging_power_kw", "fixed_cost",
    }
    vehicle_ids: set[int] = set()
    for index, row in enumerate(raw_vehicles):
        prefix = f"vehicles[{index}]"
        if not isinstance(row, dict):
            add(prefix, "必须是对象")
            continue
        for key in sorted(set(row) - allowed_vehicle_fields):
            add(f"{prefix}.{key}", "未知字段")
        if "id" not in row:
            add(f"{prefix}.id", "必填")
        vehicle_id = row.get("id")
        if type(vehicle_id) is not int or vehicle_id < 0:
            add(f"{prefix}.id", "必须是非负整数")
        elif vehicle_id in vehicle_ids:
            add(f"{prefix}.id", "车辆 ID 重复")
        else:
            vehicle_ids.add(vehicle_id)
        for key, default in vehicle_defaults.items():
            value = row.get(key, default)
            number = _finite_number(value)
            if number is None:
                add(f"{prefix}.{key}", "必须是有限数值")
            elif key in positive_vehicle_fields and number <= 0:
                add(f"{prefix}.{key}", "必须大于 0")
            elif key == "load_consumption_kwh_per_kg_km" and number < 0:
                add(f"{prefix}.{key}", "不能小于 0")
            elif key == "safety_battery_fraction" and not 0 < number < 1:
                add(f"{prefix}.{key}", "必须在 0 和 1 之间")

    cost_defaults = {
        "distance_cost_per_km": 1.2, "energy_cost_per_kwh": 0.8,
        "lateness_cost_per_minute": 8.0, "missed_customer_penalty": 10000.0,
    }
    for key in sorted(set(raw_costs) - set(cost_defaults)):
        add(f"costs.{key}", "未知字段")
    for key, default in cost_defaults.items():
        value = raw_costs.get(key, default)
        number = _finite_number(value)
        if number is None:
            add(f"costs.{key}", "必须是有限数值")
        elif number < 0:
            add(f"costs.{key}", "不能小于 0")
    return errors


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _connection(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _not_found(exc: KeyError) -> HTTPException:
    return HTTPException(404, str(exc).strip("'"))


def create_app(root: Path | None = None) -> FastAPI:
    runtime = root or Path("runtime/routeforge")
    runtime.mkdir(parents=True, exist_ok=True)
    db = runtime / "state.sqlite"
    with _connection(db) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS scenes (id TEXT, revision INTEGER, scene TEXT, PRIMARY KEY(id, revision))"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, scenario_id TEXT, revision INTEGER, payload TEXT)"
        )
    jobs = RouteJobManager(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.jobs = jobs
        await jobs.start()
        try:
            yield
        finally:
            await jobs.stop()

    app = FastAPI(title="RouteForge", lifespan=lifespan)

    def validate(scene: Any) -> dict[str, Any]:
        instance = instance_from_dict(scene)
        warnings: list[str] = []
        for customer in instance.customers:
            if all(
                customer.dry_demand > vehicle.dry_capacity_kg
                or customer.recyclable_demand > vehicle.recyclable_capacity_kg
                for vehicle in instance.vehicles
            ):
                warnings.append(f"customer {customer.id} exceeds every vehicle compartment")
        normalized = {"schema_version": 1, "coordinate_unit": "km", **instance.as_dict()}
        return {"scene": normalized, "warnings": warnings}

    def checked_scene(scene: Any) -> dict[str, Any]:
        field_errors = _scene_field_errors(scene)
        if field_errors:
            raise HTTPException(
                422,
                {
                    "error": {
                        "code": "INVALID_SCENARIO",
                        "message": f"场景有 {len(field_errors)} 处字段问题",
                        "field_errors": field_errors,
                        "retryable": False,
                    }
                },
            )
        try:
            return validate(scene)
        except (OverflowError, TypeError, ValueError) as exc:
            raise HTTPException(
                422,
                {
                    "error": {
                        "code": "INVALID_SCENARIO",
                        "message": str(exc),
                        "field_errors": [{"path": "scene", "message": str(exc)}],
                        "retryable": False,
                    }
                },
            ) from exc

    def stored_instance(scenario_id: str, revision: int) -> Any:
        with _connection(db) as con:
            row = con.execute(
                "SELECT scene FROM scenes WHERE id=? AND revision=?", (scenario_id, revision)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "scenario revision not found")
        return instance_from_dict(json.loads(row["scene"]))

    def persist_plan(
        *,
        scenario_id: str,
        revision: int,
        plan: RoutePlan,
        method: str,
        complete: bool = True,
    ) -> dict[str, Any]:
        evaluation = evaluate_plan(stored_instance(scenario_id, revision), plan)
        payload = {
            "schema_version": 1,
            "method": method,
            "seed": None,
            "iterations": 0,
            "ants": 0,
            "plan": plan.as_dict(),
            "evaluation": evaluation.as_dict(),
            "baseline_plan": None,
            "baseline_evaluation": None,
            "history": [],
            "complete": complete,
            "completed_iterations": 0,
        }
        plan_id = f"plan_{uuid.uuid4().hex}"
        with _connection(db) as con:
            con.execute(
                "INSERT INTO plans(id, scenario_id, revision, payload) VALUES (?, ?, ?, ?)",
                (plan_id, scenario_id, revision, json.dumps(payload)),
            )
        return {"plan_id": plan_id, "scenario_id": scenario_id, "revision": revision, "result": payload}

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/scenarios", status_code=201)
    def create_scene(request: SceneRequest) -> dict[str, Any]:
        checked = checked_scene(request.scene)
        scene_id = f"scene_{uuid.uuid4().hex}"
        with _connection(db) as con:
            con.execute("INSERT INTO scenes VALUES (?, 1, ?)", (scene_id, json.dumps(checked["scene"])))
        return {"scenario_id": scene_id, "revision": 1, **checked}

    @app.get("/api/v1/scenarios")
    def list_scenes(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
        """List recent immutable scene revisions so a browser can reopen them."""

        with _connection(db) as con:
            rows = con.execute(
                "SELECT id, revision, scene FROM scenes ORDER BY rowid DESC LIMIT ?", (limit + 1,)
            ).fetchall()
        has_more = len(rows) > limit
        versions = []
        for row in rows[:limit]:
            scene = json.loads(row["scene"])
            nodes = scene.get("nodes", [])
            versions.append(
                {
                    "scenario_id": row["id"],
                    "revision": row["revision"],
                    "nodes": len(nodes),
                    "customers": sum(node.get("kind") == "customer" for node in nodes),
                    "vehicles": len(scene.get("vehicles", [])),
                }
            )
        return {"scenarios": versions, "has_more": has_more}

    @app.post("/api/v1/scenarios/validate")
    def validate_draft(request: SceneRequest) -> dict[str, Any]:
        """Validate a local draft without creating a revision or a job."""

        return checked_scene(request.scene)

    @app.get("/api/v1/scenarios/{scene_id}/{revision}")
    def get_scene(scene_id: str, revision: int) -> dict[str, Any]:
        with _connection(db) as con:
            row = con.execute("SELECT scene FROM scenes WHERE id=? AND revision=?", (scene_id, revision)).fetchone()
        if row is None:
            raise HTTPException(404, "scenario revision not found")
        return {"scenario_id": scene_id, "revision": revision, "scene": json.loads(row["scene"])}

    @app.patch("/api/v1/scenarios/{scene_id}")
    def update_scene(
        scene_id: str,
        request: SceneRequest,
        if_match: int = Header(alias="If-Match"),
    ) -> dict[str, Any]:
        checked = checked_scene(request.scene)
        with _connection(db) as con:
            latest = con.execute("SELECT max(revision) FROM scenes WHERE id=?", (scene_id,)).fetchone()[0]
            if latest is None:
                raise HTTPException(404, "scenario not found")
            if latest != if_match:
                raise HTTPException(409, "scene revision changed")
            revision = if_match + 1
            con.execute("INSERT INTO scenes VALUES (?, ?, ?)", (scene_id, revision, json.dumps(checked["scene"])))
        return {"scenario_id": scene_id, "revision": revision, **checked}

    @app.post("/api/v1/solve-runs", status_code=202)
    async def create_solve_run(request: SolveRequest) -> dict[str, Any]:
        with _connection(db) as con:
            scene_row = con.execute(
                "SELECT scene FROM scenes WHERE id=? AND revision=?", (request.scenario_id, request.revision)
            ).fetchone()
        if scene_row is None:
            raise HTTPException(404, "scenario revision not found")
        customer_count = sum(node["kind"] == "customer" for node in json.loads(scene_row["scene"])["nodes"])
        try:
            job = jobs.create(
                scenario_id=request.scenario_id,
                revision=request.revision,
                algorithm=request.algorithm,
                seed=request.seed,
                iterations=request.iterations,
                ants=request.ants,
                customer_count=customer_count,
            )
        except QueueFullError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await jobs.enqueue(job.id)
        return job.as_dict()

    @app.get("/api/v1/solve-runs/{job_id}")
    def get_solve_run(job_id: str) -> dict[str, Any]:
        try:
            return jobs.get(job_id).as_dict()
        except JobNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.post("/api/v1/solve-runs/{job_id}/cancel")
    def cancel_solve_run(job_id: str) -> dict[str, Any]:
        try:
            return jobs.cancel(job_id).as_dict()
        except JobNotFoundError as exc:
            raise _not_found(exc) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/solve-runs/{job_id}/events")
    async def solve_events(
        job_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        try:
            initial_after = max(after, int(last_event_id or 0))
            jobs.get(job_id)
        except ValueError as exc:
            raise HTTPException(422, "Last-Event-ID must be an integer") from exc
        except JobNotFoundError as exc:
            raise _not_found(exc) from exc

        async def stream() -> AsyncIterator[str]:
            cursor = initial_after
            while True:
                events = jobs.events_after(job_id, cursor)
                for event in events:
                    cursor = event["seq"]
                    yield (
                        f"id: {event['seq']}\n"
                        f"event: {event['kind']}\n"
                        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    )
                status = jobs.get(job_id).status
                if status in {"cancelled", "succeeded", "failed", "interrupted"}:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.15)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/api/v1/solve-runs/{job_id}/plans")
    def solve_plans(job_id: str) -> dict[str, Any]:
        try:
            job = jobs.get(job_id)
        except JobNotFoundError as exc:
            raise _not_found(exc) from exc
        plans: list[dict[str, Any]] = []
        if job.plan_id is not None:
            plans.append({"plan_id": job.plan_id, "kind": "best", "complete": job.status == "succeeded"})
        return {"job_id": job_id, "plans": plans}

    @app.get("/api/v1/solve-runs/{job_id}/artifacts")
    def solve_artifacts(job_id: str) -> dict[str, Any]:
        try:
            return {"job_id": job_id, "artifacts": jobs.artifacts_for_job(job_id)}
        except JobNotFoundError as exc:
            raise _not_found(exc) from exc

    @app.get("/api/v1/artifacts/{artifact_id}/download")
    def download_artifact(artifact_id: str) -> FileResponse:
        artifact = jobs.artifact(artifact_id)
        if artifact is None:
            raise HTTPException(404, "artifact not found")
        path = Path(artifact["path"])
        if not path.is_file():
            raise HTTPException(404, "artifact file no longer exists")
        return FileResponse(
            path,
            media_type=artifact["content_type"],
            filename=artifact["display_name"],
        )

    @app.post("/api/v1/plans/evaluate", status_code=201)
    def evaluate_manual_plan(request: ManualPlanRequest) -> dict[str, Any]:
        instance = stored_instance(request.scenario_id, request.revision)
        if len(request.routes) != len(request.vehicle_ids):
            raise HTTPException(422, "each manual route needs one vehicle_id")
        vehicle_indices_by_id = {vehicle.id: index for index, vehicle in enumerate(instance.vehicles)}
        unknown_ids = sorted({vehicle_id for vehicle_id in request.vehicle_ids if vehicle_id not in vehicle_indices_by_id})
        if unknown_ids:
            raise HTTPException(422, f"unknown vehicle_id values: {unknown_ids}")
        plan = RoutePlan.from_lists(
            request.routes,
            [vehicle_indices_by_id[vehicle_id] for vehicle_id in request.vehicle_ids],
        )
        return persist_plan(
            scenario_id=request.scenario_id,
            revision=request.revision,
            plan=plan,
            method="manual",
        )

    @app.post("/api/v1/comparisons")
    def compare_plans(request: ComparisonRequest) -> dict[str, Any]:
        if not 1 <= len(request.plan_ids) <= 3 or len(set(request.plan_ids)) != len(request.plan_ids):
            raise HTTPException(422, "provide one to three distinct plan_ids")
        placeholders = ",".join("?" for _ in request.plan_ids)
        with _connection(db) as con:
            rows = con.execute(
                f"SELECT id, scenario_id, revision, payload FROM plans WHERE id IN ({placeholders})", request.plan_ids
            ).fetchall()
        by_id = {row["id"]: row for row in rows}
        missing = [plan_id for plan_id in request.plan_ids if plan_id not in by_id]
        if missing:
            raise HTTPException(404, f"plan not found: {missing[0]}")
        ordered = [by_id[plan_id] for plan_id in request.plan_ids]
        origins = {(row["scenario_id"], row["revision"]) for row in ordered}
        comparable = len(origins) == 1
        reasons = [] if comparable else ["plans use different scenario revisions"]
        runs = []
        for row in ordered:
            payload = json.loads(row["payload"])
            evaluation = payload["evaluation"]
            runs.append(
                {
                    "plan_id": row["id"],
                    "method": payload["method"],
                    "scenario_id": row["scenario_id"],
                    "revision": row["revision"],
                    "complete": payload["complete"],
                    "feasibility": "feasible" if evaluation["feasible"] else "search_infeasible",
                    "metrics": {
                        key: evaluation[key]
                        for key in (
                            "total_cost",
                            "distance_km",
                            "energy_kwh",
                            "lateness_minutes",
                            "vehicles_used",
                            "missing_customers",
                            "duplicate_customers",
                        )
                    },
                }
            )
        return {"comparable": comparable, "reasons": reasons, "runs": runs}

    @app.post("/api/v1/plans/{plan_id}/exports", status_code=201)
    def export_plan(plan_id: str, request: ExportRequest) -> dict[str, Any]:
        allowed = {"json", "stops_csv", "svg", "html"}
        formats = list(dict.fromkeys(request.formats))
        if not formats or any(item not in allowed for item in formats):
            raise HTTPException(422, "formats must contain json, stops_csv, svg, and/or html")
        with _connection(db) as con:
            row = con.execute(
                "SELECT scenario_id, revision, payload FROM plans WHERE id=?", (plan_id,)
            ).fetchone()
        if row is None:
            raise HTTPException(404, "plan not found")
        payload = json.loads(row["payload"])
        instance = stored_instance(row["scenario_id"], row["revision"])
        generated = {
            "json": ("plan_json", f"{plan_id}.json", "application/json"),
            "stops_csv": ("stops_csv", f"{plan_id}-stops.csv", "text/csv"),
            "svg": ("route_svg", f"{plan_id}.svg", "image/svg+xml"),
            "html": ("plan_html", f"{plan_id}.html", "text/html"),
        }
        content = {
            "json": json.dumps(
                {"plan_id": plan_id, "scenario_id": row["scenario_id"], "revision": row["revision"], "result": payload},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode() + b"\n",
            "stops_csv": stops_csv(payload),
            "svg": route_svg(instance, payload),
            "html": report_html(instance, payload, plan_id),
        }
        artifacts = []
        for item in formats:
            kind, display_name, content_type = generated[item]
            registered = jobs.register_bytes(
                owner_id=f"plan:{plan_id}",
                kind=kind,
                display_name=display_name,
                content_type=content_type,
                content=content[item],
            )
            artifacts.append(
                {key: registered[key] for key in ("artifact_id", "kind", "display_name", "content_type", "sha256", "bytes")}
            )
        return {"plan_id": plan_id, "artifacts": artifacts}

    @app.get("/api/v1/plans/{plan_id}")
    def get_plan(plan_id: str) -> dict[str, Any]:
        with _connection(db) as con:
            row = con.execute("SELECT scenario_id, revision, payload FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "plan not found")
        return {
            "plan_id": plan_id,
            "scenario_id": row["scenario_id"],
            "revision": row["revision"],
            "result": json.loads(row["payload"]),
        }

    @app.get("/api/v1/plans/{plan_id}/stops")
    def get_stops(plan_id: str) -> dict[str, Any]:
        plan = get_plan(plan_id)
        return {"plan_id": plan_id, "vehicles": plan["result"]["evaluation"]["route_details"]}

    package_static = Path(__file__).resolve().parents[1] / "static"
    web_build = (
        package_static
        if (package_static / "index.html").is_file()
        else Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    if web_build.is_dir():
        app.mount("/", StaticFiles(directory=web_build, html=True), name="workbench")
    return app
