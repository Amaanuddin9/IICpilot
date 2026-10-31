# Adaptive IICPilot Web UI

An EDA Playground-style (`edaplayground.com`) web interface for the **Adaptive IICPilot** orchestrator.
Provides real-time Server-Sent Events (SSE) streaming, Human-in-the-Loop (HITL) proposal review and editing gates, live ORFS configuration previews, and cross-session persistent memory demonstration.

## Quick Start

1. Start the web server:
   ```bash
   uvicorn adaptive_iicpilot.web.server:app --reload
   ```

2. Open your browser at:
   ```
   http://127.0.0.1:8000/
   ```

---

## 3-Step Demo Walkthrough (For Jury & Evaluation)

This sequence proves the core adaptive claim: **cross-session memory eliminates repeated routing failures and congestion re-derivation.**

### Step 1: Cold Run with Aggressive Knobs (Initial Exploration)
1. Select design `picorv32`.
2. Click **Demo Step 1 (Aggressive)** preset (sets `core_utilization=72%` and `placement_density=0.95`).
3. Set mode to **Auto**.
4. Click **Run**:
   - **Log observation**: `[EDA] run #1` fails at route with `routing congestion 0.57 exceeds limit 0.55`.
   - **Recovery**: DSE agent identifies congestion and proposes decreasing placement density. Over successive iterations, routing pressure clears and the flow converges to a clean state.
   - **Final Report**:
     - `seeded: false` (Cold start)
     - `flow_runs: >= 3`
     - `failures: >= 1`
     - `congestion_fights: >= 1`
   - Memory automatically learns and stores the clean configuration for `picorv32`.

### Step 2: Repeat Run (Cross-Session Memory Win)
1. Keep the exact same task parameters (`picorv32`, `util=72%`, `density=0.95`).
2. Click **Run** again:
   - **Log observation**: `[Memory] WARM seed found for picorv32/nangate45`.
   - The orchestrator resumes directly from the remembered clean parameters rather than guessing from the aggressive base.
   - **Final Report**:
     - `seeded: true` (Warm start)
     - `flow_runs: 1`
     - `failures: 0` (Zero failures!)
     - `congestion_fights: 0` (Zero congestion fights!)
   - This directly visualizes the persistent memory advantage.

### Step 3: Reset Memory
1. Click the **↺ Reset memory** button in the top bar.
2. The memory indicator returns to `COLD`.
3. Running `picorv32` again will revert to the cold behavior of Step 1.

---

## Human-in-the-Loop (HITL) Mode
- Toggle the top bar to **HITL**.
- When DSE produces a proposal, execution safely pauses on the proposal gate.
- The UI displays an inline interactive card with the rationale, diff, and `[memory-seeded]` badge.
- You can **Accept**, **Reject**, or directly **Edit** proposed values before approving.
