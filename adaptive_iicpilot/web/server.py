"""FastAPI backend server for Adaptive IICPilot Web UI.

Bridges the synchronous orchestrator to web SSE streaming and interactive
human-in-the-loop decision gates using per-run thread-safe queues.
Persists an InMemoryMemory instance across runs for demonstration of cross-session learning.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# Ensure compat is loaded and sys.modules aliases are registered
from . import compat
from .compat import ConsoleInterface, FlowConfig, InMemoryMemory, Orchestrator, Proposal, RunReport, Task
from ..sqlite_memory import make_memory

app = FastAPI(title="Adaptive IICPilot Web UI", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Memory instance shared by every run. SQLite by default, so learning also
# survives restarting this server (ADAPTIVE_IICPILOT_MEMORY=inmemory opts out).
SHARED_MEMORY = make_memory()

# Active sessions dictionary
SESSIONS: Dict[str, RunSession] = {}


@dataclass
class RunSession:
    run_id: str
    task: Task
    mode: str
    out_queue: queue.Queue
    decision_queue: queue.Queue
    thread: Optional[threading.Thread] = None
    is_done: bool = False
    error: Optional[str] = None


class WebInterface:
    """Duck-types ConsoleInterface: routes emit to out_queue and blocks on decide in HITL mode."""

    def __init__(self, mode: str, out_queue: queue.Queue, decision_queue: queue.Queue) -> None:
        self.mode = mode
        self.out_queue = out_queue
        self.decision_queue = decision_queue

    def emit(self, msg: str) -> None:
        self.out_queue.put({"type": "log", "text": msg})

    def decide(self, proposal: Proposal, config: FlowConfig) -> Optional[Proposal]:
        # Always log the proposal event
        self.emit(f"[DSE] proposal ({proposal.triggered_by}): {proposal.rationale}")

        if self.mode == "auto":
            # Auto mode streams without pausing
            return proposal

        # HITL mode: push proposal card event to out_queue, then BLOCK on decision_queue
        event = {
            "type": "proposal",
            "rationale": proposal.rationale,
            "triggered_by": proposal.triggered_by,
            "changes": dict(proposal.changes),
            "seeded_from_memory": proposal.seeded_from_memory,
        }
        self.out_queue.put(event)

        # Block until user provides decision via POST /decide/{run_id}
        decision_data = self.decision_queue.get()
        decision = decision_data.get("decision", "accept")

        if decision == "accept":
            self.emit(f"[Control] HITL decision: Accepted proposal ({proposal.triggered_by})")
            return proposal
        elif decision == "reject":
            self.emit(f"[Control] HITL decision: Rejected proposal ({proposal.triggered_by})")
            return None
        elif decision == "edit":
            changes = decision_data.get("changes", {})
            self.emit(f"[Control] HITL decision: Edited proposal changes to {changes}")
            return Proposal(
                changes=changes,
                rationale=proposal.rationale,
                triggered_by=proposal.triggered_by,
                seeded_from_memory=proposal.seeded_from_memory,
            )
        return proposal


def _run_worker(session: RunSession, orchestrator: Orchestrator, task: Task) -> None:
    try:
        report: RunReport = orchestrator.run(task)
        m = report.best.metrics
        report_data = {
            "type": "report",
            "seeded": report.seeded,
            "flow_runs": report.flow_runs,
            "failures": report.failures,
            "congestion_fights": report.congestion_fights,
            "reached_stage": report.best.reached_stage,
            "ok": report.best.ok,
            "failure": report.best.failure,
            "wns_ns": round(m.wns_ns, 4),
            "tns_ns": round(m.tns_ns, 4),
            "congestion": round(m.congestion, 4),
            "area_um2": round(m.area_um2, 1),
            "power_mw": round(m.power_mw, 2),
            "ppa_score": m.ppa_score(),
            "final_config": {
                "design": report.best.config.design,
                "tech": report.best.config.tech,
                "clock_period_ns": report.best.config.clock_period_ns,
                "core_utilization": report.best.config.core_utilization,
                "placement_density": report.best.config.placement_density,
                "aspect_ratio": report.best.config.aspect_ratio,
                "floorplan": report.best.config.floorplan,
            },
            "floorplan_stage": report.floorplan,
        }
        session.out_queue.put(report_data)
    except Exception as exc:
        session.error = str(exc)
        session.out_queue.put({"type": "log", "text": f"[Control] ERROR during execution: {exc}"})
        session.out_queue.put({"type": "error", "error": str(exc)})
    finally:
        session.is_done = True
        session.out_queue.put(None)  # Sentinel to indicate end of stream


class RunRequest(BaseModel):
    design: str = "picorv32"
    tech: str = "nangate45"
    target_stage: str = "route"
    clock_period_ns: float = 2.5
    core_utilization: float = 72.0
    placement_density: float = 0.95
    aspect_ratio: float = 1.0
    max_iters: int = 6
    mode: str = "auto"  # "auto" or "hitl"
    # Floorplan stage ahead of DSE: "apply" (FloorplanAgent's measured choice),
    # "apply_if_cached" or "stock". An aspect ratio other than 1.0 in the
    # request counts as the engineer's own floorplan and is kept.
    floorplan_policy: str = "apply"


class DecideRequest(BaseModel):
    decision: str = Field(..., description="'accept', 'reject', or 'edit'")
    changes: Optional[Dict[str, Any]] = None


@app.post("/run")
async def start_run(req: RunRequest):
    run_id = uuid.uuid4().hex[:12]
    out_q: queue.Queue = queue.Queue()
    dec_q: queue.Queue = queue.Queue()

    base_config = FlowConfig(
        design=req.design,
        tech=req.tech,
        clock_period_ns=req.clock_period_ns,
        core_utilization=req.core_utilization,
        placement_density=req.placement_density,
        aspect_ratio=req.aspect_ratio,
    )

    task = Task(
        design=req.design,
        tech=req.tech,
        target_stage=req.target_stage,
        base_config=base_config,
        max_iters=req.max_iters,
        floorplan_policy=req.floorplan_policy,
    )

    web_interface = WebInterface(mode=req.mode.lower(), out_queue=out_q, decision_queue=dec_q)
    orchestrator = Orchestrator(memory=SHARED_MEMORY, interface=web_interface)

    session = RunSession(
        run_id=run_id,
        task=task,
        mode=req.mode.lower(),
        out_queue=out_q,
        decision_queue=dec_q,
    )

    thread = threading.Thread(
        target=_run_worker,
        args=(session, orchestrator, task),
        name=f"orchestrator-run-{run_id}",
        daemon=True,
    )
    session.thread = thread
    SESSIONS[run_id] = session
    thread.start()

    return {"run_id": run_id, "status": "started"}


@app.get("/stream/{run_id}")
async def stream_run(run_id: str):
    session = SESSIONS.get(run_id)
    if not session:
        raise HTTPException(status_code=404, detail="Run not found")

    def event_generator():
        while True:
            try:
                item = session.out_queue.get(timeout=0.2)
            except queue.Empty:
                if session.is_done and session.out_queue.empty():
                    yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                    break
                continue

            if item is None:
                yield f"data: {json.dumps({'type': 'stream_end'})}\n\n"
                break
            yield f"data: {json.dumps(item)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/decide/{run_id}")
async def decide_run(run_id: str, req: DecideRequest):
    session = SESSIONS.get(run_id)
    if not session:
        raise HTTPException(status_code=404, detail="Run not found")

    session.decision_queue.put({"decision": req.decision.lower(), "changes": req.changes or {}})
    return {"ok": True, "decision": req.decision}


@app.post("/reset-memory")
async def reset_memory():
    global SHARED_MEMORY
    # Persistent memory is wiped in place; a fresh object would leave the
    # learned rows on disk and the next run would still come back warm.
    if hasattr(SHARED_MEMORY, "reset"):
        SHARED_MEMORY.reset()
    else:
        SHARED_MEMORY = InMemoryMemory()
    return {"ok": True, "message": "Memory reset to cold state"}


@app.get("/api/memory")
async def get_memory_status():
    if hasattr(SHARED_MEMORY, "describe"):
        return SHARED_MEMORY.describe()

    designs = {}
    for key, val in SHARED_MEMORY._designs.items():
        designs[key] = {
            "has_best_config": bool(val.get("best_config")),
            "best_config": val.get("best_config"),
            "best_score": val.get("best_score", 0.0),
            "heuristics": val.get("heuristics", []),
            "run_count": val.get("run_count", 0),
            "failure_count": val.get("failure_count", 0),
        }
    return {
        "total_runs": SHARED_MEMORY.run_count,
        "total_failures": SHARED_MEMORY.failure_count,
        "designs": designs,
    }


@app.get("/")
async def serve_index():
    index_path = Path(__file__).parent / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_path, media_type="text/html")
