"""Read a config.mk and say how each variable is assigned.

The distinction matters because it decides how an override has to be passed:
an environment variable only beats `?=`; plain `=` / `:=` / `+=` need a make
command-line variable. The floorplan runner passes everything on the command
line anyway, but the report makes the reason visible per design.
"""
from __future__ import annotations

import re
from typing import Optional

_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?P<op>\?=|:=|\+=|=)\s*(?P<value>.*)$"
)

# Variables the floorplan agent drives or that interact with what it drives.
FLOORPLAN_VARS = (
    "CORE_UTILIZATION",
    "CORE_ASPECT_RATIO",
    "CORE_MARGIN",
    "DIE_AREA",
    "CORE_AREA",
    "FLOORPLAN_DEF",
    "IO_CONSTRAINTS",
    "PLACE_PINS_ARGS",
    "MACRO_PLACE_HALO",
    "MACRO_PLACE_CHANNEL",
    "MACRO_PLACEMENT_TCL",
    "PLACE_DENSITY",
    "PLACE_DENSITY_LB_ADDON",
    "ADDITIONAL_LEFS",
)


def scan(text: str) -> dict[str, dict]:
    """name -> {"op", "value", "line"} for every assignment, last one wins
    (which is how make resolves repeated plain assignments)."""
    joined: list[tuple[int, str]] = []
    buffer, start = "", 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        if buffer.strip():
            joined.append((start, buffer))
        buffer = ""
    if buffer.strip():
        joined.append((start, buffer))

    found: dict[str, dict] = {}
    for number, line in joined:
        match = _ASSIGN_RE.match(line)
        if not match:
            continue
        found[match.group("name")] = {
            "op": match.group("op"),
            "value": " ".join(match.group("value").split()),
            "line": number,
        }
    return found


def classify(text: str, names: Optional[tuple] = None) -> dict:
    """Split floorplan-relevant variables by how they must be overridden."""
    names = names or FLOORPLAN_VARS
    assigned = scan(text)
    hard, soft, unset = {}, {}, []
    for name in names:
        entry = assigned.get(name)
        if entry is None:
            unset.append(name)
        elif entry["op"] == "?=":
            soft[name] = entry["value"]
        else:
            hard[name] = {"op": entry["op"], "value": entry["value"]}
    return {
        # Must go on the make command line: the environment cannot beat these.
        "command_line_required": hard,
        # `?=`: environment or command line both work.
        "env_overridable": soft,
        "not_set_by_design": unset,
    }
