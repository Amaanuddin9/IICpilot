# Adaptive IICPilot — VS Code extension

A chat panel over the existing Adaptive IICPilot agents. A local Ollama model
decides which tool to call; the DSE agent picks physical-design parameters and
the EDA agent runs the OpenROAD flow. Every number shown in the panel comes
back from a tool call — the extension itself computes nothing.

This is a presentation layer only. It talks to the Python package over a JSON
protocol (`python -m adaptive_iicpilot.chat --jsonl`) and does not modify the
agents, orchestrator, memory or tools.

## Requirements

- VS Code connected to WSL (the extension host must run in the same Linux
  environment as ORFS, Ollama and Python).
- `ollama serve` running with the model pulled: `ollama pull llama3.1:8b`.
- The `adaptive_iicpilot` package importable by the configured interpreter,
  with `openai` installed (`pip install openai`).

## Build

```bash
cd vscode-extension
npm install
npm run compile      # tsc -> out/
npm run package      # -> adaptive-iicpilot-1.0.0.vsix
```

## Install

In VS Code: **Extensions** view → `…` menu → **Install from VSIX…** → pick
`adaptive-iicpilot-1.0.0.vsix`. Then open the IICPilot icon in the activity
bar, or run **Adaptive IICPilot: Open Chat** from the command palette.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `adaptiveIicpilot.pythonPath` | `python3` | Interpreter that runs the backend |
| `adaptiveIicpilot.repoPath` | *(workspace root)* | Where to spawn the backend |
| `adaptiveIicpilot.ollamaModel` | `llama3.1:8b` | Passed as `OLLAMA_MODEL` |
| `adaptiveIicpilot.backend` | `orfs` | Passed as `ADAPTIVE_IICPILOT_BACKEND` |
| `adaptiveIicpilot.hitl` | `false` | Approve each DSE proposal before it runs |

## Using it

Type a request, e.g.:

- `run ibex at 80% utilization` — one configuration, returns a metrics card.
- `optimize ibex` — searches for the smallest die with congestion ≤ 1.0. Run it
  twice: the second is warm from persistent memory and needs fewer flow runs.
- `why did util 90 congest?` — explained from measured data, no invented numbers.

Agent activity streams as labelled monospace rows (Control / DSE / EDA / Memory
/ Tool). Results render as a metrics card; the status bar shows the model and
whether memory is WARM or COLD.

**HITL mode.** Tick *Approve each proposal* and the DSE agent pauses on every
proposal with Accept / Edit / Reject. Editing a value is recorded as an
engineer correction in persistent memory — confirm with:

```bash
python -m adaptive_iicpilot.memory_cli show --design ibex
```

**Reset memory** wipes the learned state so the next `optimize` starts cold.

### Floorplanning

- `compare floorplans for ibex` — core aspect ratio and I/O pin strategies
  (ORFS default, buses grouped per side, inputs left / outputs right).
- `compare macro placements for tinyRocket` — RTL-MP with a halo sweep vs
  manual periphery / edge-cluster placements, legality-checked before running.

The `floorplan` tool returns a ranked table and shows the best floorplan as an
image card (die, core, macros with halos, pins colored by group). Rows tagged
`[Floorplan]` include the override proof from each run's own logs. In HITL
mode the full-flow proposal card shows the floorplan before you approve it;
editing a macro's `x`/`y` turns it into a manual placement and is remembered as
an engineer correction.

A chat turn only replays measured candidates (gcd excepted, which is fast
enough to run live). Measure a design from the shell first; results are cached
per variant under `.floorplan_runs/`:

```bash
python -m adaptive_iicpilot.floorplan --design ibex --studies aspect,pins --full per_family
python -m adaptive_iicpilot.floorplan --design tinyRocket --full per_family
```

Each candidate runs in its own `FLOW_VARIANT=fp_<stage>_<hash>`, so a sweep in
the `base` variant is never touched. Set `ADAPTIVE_IICPILOT_FLOORPLAN_LIVE=1`
to let the chat run uncached candidates (slow: minutes to hours).

## Troubleshooting

- *"Cannot reach Ollama"* in the panel: start `ollama serve`.
- *Backend stopped*: use the **Restart backend** button in the panel. Python
  stderr and the spawn command line are in the **Adaptive IICPilot** output
  channel (**Logs** button).
- Nothing happens on send: check `adaptiveIicpilot.pythonPath` and
  `adaptiveIicpilot.repoPath` point at the interpreter and repo inside WSL.
