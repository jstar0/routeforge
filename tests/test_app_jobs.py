from __future__ import annotations

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from routeforge.app.jobs import RouteJobManager
from routeforge.app.main import create_app
from routeforge.instance import generate_instance
from routeforge.solver import SolverConfig, solve


def _wait_for_terminal(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        payload = client.get(f"/api/v1/solve-runs/{job_id}").json()
        if payload["status"] in {"succeeded", "cancelled", "failed", "interrupted"}:
            return payload
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal state")


def test_aco_cancel_returns_last_completed_iteration() -> None:
    instance = generate_instance(customers=12, chargers=3, vehicles=3, seed=8)
    probes = 0

    def cancel_after_second_iteration() -> bool:
        nonlocal probes
        probes += 1
        return probes >= 2

    result = solve(
        instance,
        seed=8,
        config=SolverConfig(iterations=8, ants=2),
        should_cancel=cancel_after_second_iteration,
    )
    assert not result.complete
    assert len(result.history) == 2
    assert result.as_dict()["completed_iterations"] == 2


def test_durable_job_api_replays_events_and_keeps_plan(tmp_path) -> None:
    app = create_app(tmp_path / "runtime")
    scene = generate_instance(customers=12, chargers=3, vehicles=3, seed=21).as_dict()
    with TestClient(app) as client:
        created = client.post("/api/v1/scenarios", json={"scene": scene})
        assert created.status_code == 201
        scenario = created.json()
        response = client.post(
            "/api/v1/solve-runs",
            json={
                "scenario_id": scenario["scenario_id"],
                "revision": scenario["revision"],
                "algorithm": "aco",
                "seed": 21,
                "iterations": 4,
                "ants": 2,
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        job = _wait_for_terminal(client, job_id)
        assert job["status"] == "succeeded"
        assert job["progress"] == {"done": 4, "total": 4, "unit": "iterations"}
        replay = client.get(f"/api/v1/solve-runs/{job_id}/events?after=0")
        assert replay.status_code == 200
        assert "event: queued" in replay.text
        assert "event: iteration" in replay.text
        assert "event: succeeded" in replay.text
        resumed = client.get(
            f"/api/v1/solve-runs/{job_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert resumed.status_code == 200
        assert "id: 1\n" not in resumed.text
        plan_id = job["plan_id"]
        assert isinstance(plan_id, str)
        plan = client.get(f"/api/v1/plans/{plan_id}").json()
        assert plan["revision"] == scenario["revision"]
        assert plan["result"]["complete"] is True
        assert client.get(f"/api/v1/plans/{plan_id}/stops").status_code == 200
        artifacts = client.get(f"/api/v1/solve-runs/{job_id}/artifacts").json()["artifacts"]
        assert len(artifacts) == 1
        downloaded = client.get(f"/api/v1/artifacts/{artifacts[0]['artifact_id']}/download")
        assert downloaded.status_code == 200
        assert downloaded.json()["complete"] is True


def test_restart_marks_active_job_interrupted_and_keeps_event_history(tmp_path) -> None:
    database = tmp_path / "runtime" / "state.sqlite"
    manager = RouteJobManager(database)
    manager.initialize()
    with sqlite3.connect(database) as con:
        con.execute(
            "INSERT INTO jobs(id, scenario_id, revision, config, status) VALUES (?, ?, ?, ?, ?)",
            (
                "job_abandoned",
                "scene_example",
                1,
                json.dumps({"algorithm": "aco", "seed": 1, "iterations": 4, "ants": 2}),
                "running",
            ),
        )
    restarted = RouteJobManager(database)
    restarted.initialize()
    assert restarted.get("job_abandoned").status == "interrupted"
    assert restarted.events_after("job_abandoned", 0)[-1]["kind"] == "interrupted"


def test_api_rejects_configuration_that_would_freeze_an_interactive_iteration(tmp_path) -> None:
    app = create_app(tmp_path / "runtime")
    scene = generate_instance(customers=200, chargers=20, vehicles=20, seed=22).as_dict()
    with TestClient(app) as client:
        scenario = client.post("/api/v1/scenarios", json={"scene": scene}).json()
        response = client.post(
            "/api/v1/solve-runs",
            json={
                "scenario_id": scenario["scenario_id"],
                "revision": scenario["revision"],
                "algorithm": "aco",
                "iterations": 100,
                "ants": 100,
            },
        )
    assert response.status_code == 422
    assert "one interactive ACO iteration" in response.json()["detail"]


def test_draft_validation_is_non_persistent_and_returns_structured_error(tmp_path) -> None:
    app = create_app(tmp_path / "runtime")
    valid = generate_instance(customers=2, chargers=1, vehicles=1, seed=23).as_dict()
    invalid = {**valid, "coordinate_unit": "miles"}
    invalid["nodes"] = [dict(node) for node in valid["nodes"]]
    invalid["nodes"][1]["kind"] = []
    invalid["nodes"][1]["x"] = 10**500
    with TestClient(app) as client:
        accepted = client.post("/api/v1/scenarios/validate", json={"scene": valid})
        rejected = client.post("/api/v1/scenarios/validate", json={"scene": invalid})
        non_object = client.post("/api/v1/scenarios/validate", json={"scene": []})
    assert accepted.status_code == 200
    assert accepted.json()["scene"]["schema_version"] == 1
    assert accepted.json()["scene"]["coordinate_unit"] == "km"
    assert rejected.status_code == 422
    error = rejected.json()["detail"]["error"]
    assert error["code"] == "INVALID_SCENARIO"
    assert error["retryable"] is False
    assert {item["path"] for item in error["field_errors"]} >= {
        "coordinate_unit", "nodes[1].kind", "nodes[1].x"
    }
    assert non_object.status_code == 422
    assert non_object.json()["detail"]["error"]["field_errors"] == [
        {"path": "scene", "message": "必须是 JSON 对象"}
    ]


def test_saved_scenario_listing_exposes_immutable_revisions(tmp_path) -> None:
    app = create_app(tmp_path / "runtime")
    scene = generate_instance(customers=2, chargers=1, vehicles=1, seed=25).as_dict()
    with TestClient(app) as client:
        created = client.post("/api/v1/scenarios", json={"scene": scene}).json()
        changed = json.loads(json.dumps(scene))
        changed["nodes"][1]["x"] += 1.0
        updated = client.patch(
            f"/api/v1/scenarios/{created['scenario_id']}",
            headers={"If-Match": "1"},
            json={"scene": changed},
        )
        listed = client.get("/api/v1/scenarios?limit=10")
        old_version = client.get(f"/api/v1/scenarios/{created['scenario_id']}/1")
        new_version = client.get(f"/api/v1/scenarios/{created['scenario_id']}/2")

    assert updated.status_code == 200
    assert listed.status_code == 200
    assert [(item["scenario_id"], item["revision"]) for item in listed.json()["scenarios"]] == [
        (created["scenario_id"], 2),
        (created["scenario_id"], 1),
    ]
    assert old_version.json()["scene"]["nodes"][1]["x"] != new_version.json()["scene"]["nodes"][1]["x"]


def test_manual_re_evaluation_and_comparison_use_stored_revision(tmp_path) -> None:
    app = create_app(tmp_path / "runtime")
    scene = generate_instance(customers=2, chargers=1, vehicles=1, seed=24).as_dict()
    with TestClient(app) as client:
        scenario = client.post("/api/v1/scenarios", json={"scene": scene}).json()
        manual = client.post(
            "/api/v1/plans/evaluate",
            json={
                "scenario_id": scenario["scenario_id"],
                "revision": 1,
                "routes": [[0, 2, 1, 0]],
                "vehicle_ids": [0],
            },
        )
        assert manual.status_code == 201
        result = manual.json()["result"]
        assert result["method"] == "manual"
        assert result["evaluation"]["route_details"][0]["route"] == [0, 2, 1, 0]
        second = client.post(
            "/api/v1/plans/evaluate",
            json={
                "scenario_id": scenario["scenario_id"],
                "revision": 1,
                "routes": [[0, 1, 2, 0]],
                "vehicle_ids": [0],
            },
        ).json()
        comparison = client.post(
            "/api/v1/comparisons",
            json={"plan_ids": [manual.json()["plan_id"], second["plan_id"]]},
        )
        exported = client.post(
            f"/api/v1/plans/{manual.json()['plan_id']}/exports",
            json={"formats": ["json", "stops_csv", "svg", "html"]},
        )
        artifacts = exported.json()["artifacts"]
        csv_export = next(item for item in artifacts if item["kind"] == "stops_csv")
        downloaded = client.get(f"/api/v1/artifacts/{csv_export['artifact_id']}/download")
    assert comparison.status_code == 200
    assert comparison.json()["comparable"] is True
    assert [run["method"] for run in comparison.json()["runs"]] == ["manual", "manual"]
    assert exported.status_code == 201
    assert {artifact["kind"] for artifact in artifacts} == {"plan_json", "stops_csv", "route_svg", "plan_html"}
    assert downloaded.status_code == 200
    assert "battery_before_kwh" in downloaded.content.decode("utf-8-sig")
