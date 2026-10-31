"""FloorplanAgent: classic floorplanning on top of a real ORFS install.

Covers core shape (aspect ratio, margin, explicit die/core area), I/O pin
placement (generated IO_CONSTRAINTS Tcl per candidate) and hard macro
placement (RTL-MP with a halo sweep, or a generated MACRO_PLACEMENT_TCL that
is legality-checked before anything runs).

Every run lives in its own FLOW_VARIANT (a hash of the design, every knob and
the stage it runs to), so it can never touch the "base" variant a clock sweep
may be using, or another candidate's directories.
"""
from .knobs import FloorplanKnobs, Stage

__all__ = ["FloorplanAgent", "FloorplanKnobs", "Stage"]


def __getattr__(name: str):
    # Lazy, so the pure-Python pieces (knobs, IO Tcl, legality, SVG) import
    # without pulling in the agent and its memory.
    if name == "FloorplanAgent":
        from .agent import FloorplanAgent

        return FloorplanAgent
    raise AttributeError(name)
