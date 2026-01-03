"""Durable single-worker RouteForge solve jobs.

The queue itself is deliberately in process for the local workbench, while
SQLite is the authority for every transition, event, and generated plan.  This
means a browser reconnect can replay events and a service restart can describe
an abandoned calculation honestly instead of silently losing it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..instance import instance_from_dict
from ..solver import SolverConfig, solve, solve_greedy

ACTIVE_STATUSES = ("queued", "running", "cancelling")
TERMINAL_STATUSES = ("cancelled", "succeeded", "failed", "interrupted")
MAX_ANTS_PER_INTERACTIVE_ITERATION = 1_200
MAX_INTERACTIVE_ANT_CUSTOMER_STEPS = 24_000


class QueueFullError(RuntimeError):
    """Raised when the local workbench already has five waiting calculations."""


class JobNotFoundError(KeyError):
    """Raised for an unknown solve job id."""


@dataclass(frozen=True, slots=True)
class SolveJob:
    id: str
    scenario_id: str
    revision: int
    algorithm: str
    seed: int
    iterations: int
    ants: int
    status: str
    completed_iterations: int
    plan_id: str | None
    error: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> SolveJob:
        config = json.loads(row["config"])
        return cls(
            id=row["id"],
            scenario_id=row["scenario_id"],
            revision=row["revision"],
            algorithm=config["algorithm"],
            seed=config["seed"],
            iterations=config["iterations"],
            ants=config["ants"],
            status=row["status"],
            completed_iterations=row["completed_iterations"],
            plan_id=row["plan_id"],
            error=row["error"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "scenario_id": self.scenario_id,
            "revision": self.revision,
            "algorithm": self.algorithm,
            "seed": self.seed,
            "status": self.status,
            "capabilities": {"pause": False, "resume": False, "cancel": self.status in {"queued", "running"}},
            "progress": {
                "done": self.completed_iterations,
                "total": self.iterations,
                "unit": "iterations",
            },
            "plan_id": self.plan_id,
            "error": self.error,
        }


class RouteJobManager:
    """One durable worker for a RouteForge runtime directory."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.artifact_dir = db_path.parent / "artifacts"
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.worker: asyncio.Task[None] | None = None

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    config TEXT NOT NULL,
                    status TEXT NOT NULL,
                    completed_iterations INTEGER NOT NULL DEFAULT 0,
                    plan_id TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(job_id) REFERENCES jobs(id)
                );
                """
            )
            abandoned = con.execute(
                "SELECT id FROM jobs WHERE status IN ('running', 'cancelling')"
            ).fetchall()
            con.execute(
                "UPDATE jobs SET status='interrupted', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                "WHERE status IN ('running', 'cancelling')"
            )
            for row in abandoned:
                self._append_event(con, row["id"], "interrupted", {"reason": "service restarted"})

    async def start(self) -> None:
        self.initialize()
        for job in self._queued_jobs():
            self.queue.put_nowait(job.id)
        self.worker = asyncio.create_task(self._work(), name="routeforge-solve-worker")

    async def stop(self) -> None:
        if self.worker is None:
            return
        await self.queue.put(None)
        await self.worker
        self.worker = None

    def _queued_jobs(self) -> list[SolveJob]:
        with self._connection() as con:
            rows = con.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id").fetchall()
        return [SolveJob.from_row(row) for row in rows]

    def _append_event(self, con: sqlite3.Connection, job_id: str, kind: str, payload: dict[str, Any]) -> int:
        cursor = con.execute(
            "INSERT INTO job_events(job_id, kind, payload) VALUES (?, ?, ?)",
            (job_id, kind, json.dumps(payload, separators=(",", ":"))),
        )
        return int(cursor.lastrowid)

    def create(
        self,
        *,
        scenario_id: str,
        revision: int,
        algorithm: str,
        seed: int,
        iterations: int,
        ants: int,
        customer_count: int,
    ) -> SolveJob:
        if algorithm not in {"aco", "greedy"}:
            raise ValueError("algorithm must be 'aco' or 'greedy'")
        if not (1 <= iterations <= 500 and 1 <= ants <= 100):
            raise ValueError("iterations must be 1–500 and ants must be 1–100")
        if algorithm == "aco":
            ant_customer_steps = customer_count * ants
            if ant_customer_steps > MAX_ANTS_PER_INTERACTIVE_ITERATION:
                raise ValueError(
                    "configuration would make one interactive ACO iteration too slow; "
                    "reduce ants or customers"
                )
            if ant_customer_steps * iterations > MAX_INTERACTIVE_ANT_CUSTOMER_STEPS:
                raise ValueError(
                    "configuration exceeds the local interactive ACO work budget; reduce iterations, ants, or customers"
                )
        job_id = f"job_{uuid.uuid4().hex}"
        config = {"algorithm": algorithm, "seed": seed, "iterations": iterations, "ants": ants}
        with self._connection() as con:
            waiting = con.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            if waiting >= 5:
                raise QueueFullError("at most five solve jobs may wait in the local queue")
            con.execute(
                "INSERT INTO jobs(id, scenario_id, revision, config, status) VALUES (?, ?, ?, ?, 'queued')",
                (job_id, scenario_id, revision, json.dumps(config)),
            )
            self._append_event(con, job_id, "queued", {"status": "queued"})
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return SolveJob.from_row(row)

    async def enqueue(self, job_id: str) -> None:
        await self.queue.put(job_id)

    def get(self, job_id: str) -> SolveJob:
        with self._connection() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return SolveJob.from_row(row)

    def cancel(self, job_id: str) -> SolveJob:
        with self._connection() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            job = SolveJob.from_row(row)
            if job.status == "queued":
                con.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=CURRENT_TIMESTAMP, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_id,),
                )
                self._append_event(con, job_id, "cancelled", {"completed_iterations": 0})
            elif job.status == "running":
                con.execute("UPDATE jobs SET status='cancelling', updated_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
                self._append_event(con, job_id, "cancelling", {"message": "finishing current iteration"})
            elif job.status not in TERMINAL_STATUSES:
                raise RuntimeError(f"cannot cancel job in state {job.status}")
        return self.get(job_id)

    def events_after(self, job_id: str, after: int) -> list[dict[str, Any]]:
        self.get(job_id)
        with self._connection() as con:
            rows = con.execute(
                "SELECT seq, kind, payload, created_at FROM job_events WHERE job_id=? AND seq>? ORDER BY seq",
                (job_id, after),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def artifact(self, artifact_id: str) -> sqlite3.Row | None:
        with self._connection() as con:
            return con.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()

    def artifacts_for_job(self, job_id: str) -> list[dict[str, Any]]:
        self.get(job_id)
        with self._connection() as con:
            rows = con.execute(
                "SELECT id, kind, display_name, content_type, bytes, sha256 FROM artifacts WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
        return [dict(row) | {"artifact_id": row["id"]} for row in rows]

    def register_bytes(
        self,
        *,
        owner_id: str,
        kind: str,
        display_name: str,
        content_type: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Atomically write and register an export owned by a persisted plan."""

        artifact_id = f"artifact_{uuid.uuid4().hex}"
        destination = self.artifact_dir / f"{artifact_id}-{display_name}"
        temporary = self.artifact_dir / f".{artifact_id}.tmp"
        temporary.write_bytes(content)
        os.replace(temporary, destination)
        registered = {
            "artifact_id": artifact_id,
            "kind": kind,
            "display_name": display_name,
            "content_type": content_type,
            "path": str(destination),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
        with self._connection() as con:
            con.execute(
                "INSERT INTO artifacts(id, job_id, kind, display_name, content_type, path, sha256, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    owner_id,
                    kind,
                    display_name,
                    content_type,
                    registered["path"],
                    registered["sha256"],
                    registered["bytes"],
                ),
            )
        return registered

    def _register_result_artifact(self, job_id: str, plan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        destination = self.artifact_dir / f"{plan_id}.json"
        temporary = self.artifact_dir / f".{plan_id}.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
        registered = {
            "artifact_id": artifact_id,
            "kind": "plan_result",
            "display_name": f"{plan_id}.json",
            "content_type": "application/json",
            "path": str(destination),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }
        with self._connection() as con:
            con.execute(
                "INSERT INTO artifacts(id, job_id, kind, display_name, content_type, path, sha256, bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    job_id,
                    registered["kind"],
                    registered["display_name"],
                    registered["content_type"],
                    registered["path"],
                    registered["sha256"],
                    registered["bytes"],
                ),
            )
        return registered

    async def _work(self) -> None:
        while True:
            job_id = await self.queue.get()
            try:
                if job_id is None:
                    return
                await asyncio.to_thread(self._run, job_id)
            finally:
                self.queue.task_done()

    def _claim(self, job_id: str) -> SolveJob | None:
        with self._connection() as con:
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "queued":
                return None
            con.execute(
                "UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
            self._append_event(con, job_id, "running", {"status": "running"})
        return self.get(job_id)

    def _cancel_requested(self, job_id: str) -> bool:
        with self._connection() as con:
            row = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row is not None and row["status"] == "cancelling"

    def _record_iteration(self, job_id: str, total: int) -> Callable[[int, Any, Any], None]:
        def record(iteration: int, best: Any, baseline: Any) -> None:
            payload = {
                "completed_iterations": iteration,
                "total_iterations": total,
                "best_cost": round(best.total_cost, 6),
                "best_feasible": best.feasible,
                "baseline_cost": round(baseline.total_cost, 6),
            }
            with self._connection() as con:
                con.execute(
                    "UPDATE jobs SET completed_iterations=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (iteration, job_id),
                )
                self._append_event(con, job_id, "iteration", payload)

        return record

    def _run(self, job_id: str) -> None:
        job = self._claim(job_id)
        if job is None:
            return
        try:
            with self._connection() as con:
                scene_row = con.execute(
                    "SELECT scene FROM scenes WHERE id=? AND revision=?", (job.scenario_id, job.revision)
                ).fetchone()
            if scene_row is None:
                raise ValueError("scenario revision no longer exists")
            instance = instance_from_dict(json.loads(scene_row["scene"]))
            if job.algorithm == "greedy":
                result = solve_greedy(instance, seed=job.seed)
                with self._connection() as con:
                    con.execute(
                        "UPDATE jobs SET completed_iterations=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (job_id,),
                    )
            else:
                config = SolverConfig(iterations=job.iterations, ants=job.ants)
                result = solve(
                    instance,
                    seed=job.seed,
                    config=config,
                    on_iteration=self._record_iteration(job_id, config.iterations),
                    should_cancel=lambda: self._cancel_requested(job_id),
                )
            plan_id = f"plan_{uuid.uuid4().hex}"
            payload = result.as_dict()
            final_status = "succeeded" if result.complete else "cancelled"
            with self._connection() as con:
                con.execute(
                    "INSERT INTO plans(id, scenario_id, revision, payload) VALUES (?, ?, ?, ?)",
                    (plan_id, job.scenario_id, job.revision, json.dumps(payload)),
                )
            artifact = self._register_result_artifact(job_id, plan_id, payload)
            with self._connection() as con:
                con.execute(
                    "UPDATE jobs SET status=?, plan_id=?, finished_at=CURRENT_TIMESTAMP, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (final_status, plan_id, job_id),
                )
                self._append_event(
                    con,
                    job_id,
                    final_status,
                    {
                        "plan_id": plan_id,
                        "artifact_id": artifact["artifact_id"],
                        "complete": result.complete,
                        "completed_iterations": len(result.history),
                        "feasibility": "feasible" if result.evaluation.feasible else "search_infeasible",
                    },
                )
        except Exception as exc:  # keep a safe error for the local UI, not a stack trace
            with self._connection() as con:
                con.execute(
                    "UPDATE jobs SET status='failed', error=?, finished_at=CURRENT_TIMESTAMP, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(exc), job_id),
                )
                self._append_event(con, job_id, "failed", {"message": str(exc)})
