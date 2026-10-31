"""The Claude-Code-style interface: a live, labeled, streaming view of
every agent's activity, driven by ControlAgent.

Every line ControlAgent emits is tagged "[Agent] ...", exactly like
EDAAgent's existing status lines; this module's only job is to color
those tags and route them into a scrolling transcript, plus bridge the
HITL gate (which blocks a worker thread) to a modal the user can answer
without freezing the UI thread.
"""
from __future__ import annotations

import json
import threading
from typing import Optional

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static, TextArea

from .control_agent import ControlAgent
from .eda_interface import EDAInterface
from .llm_interface import LLMPort
from .memory import JSONMemory
from .types import FlowConfig, Proposal

_TAG_COLORS = {
    "Control": "white",
    "EDA": "cyan",
    "DSE": "yellow",
    "LLM": "magenta",
    "Memory": "green",
}


def _colorize(line: str) -> str:
    if line.startswith("[") and "]" in line:
        tag = line[1 : line.index("]")]
        color = _TAG_COLORS.get(tag)
        if color:
            rest = line[line.index("]") + 1 :]
            return f"[bold {color}][{tag}][/][{color}]{rest}[/]"
    return line


class HITLModal(ModalScreen[Optional[dict]]):
    """Blocking-from-the-worker-thread's-perspective prompt: accept the
    proposal as-is, edit its changes as JSON and accept that, or reject."""

    DEFAULT_CSS = """
    HITLModal {
        align: center middle;
    }
    #hitl-box {
        width: 70%;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(self, proposal: Proposal, config: FlowConfig, comment: str) -> None:
        super().__init__()
        self.proposal = proposal
        self.comment = comment

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical

        with Vertical(id="hitl-box"):
            yield Static(f"HITL gate: {self.proposal.triggered_by}")
            yield Static(self.proposal.rationale)
            yield Static(f"LLM: {self.comment}")
            yield TextArea(json.dumps(self.proposal.changes), id="changes")
            with Horizontal():
                yield Button("Accept", id="accept", variant="success")
                yield Button("Reject", id="reject", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reject":
            self.dismiss(None)
            return
        text_area = self.query_one("#changes", TextArea)
        try:
            changes = json.loads(text_area.text)
        except json.JSONDecodeError:
            changes = self.proposal.changes
        self.dismiss({"changes": changes})


class IICPilotApp(App):
    TITLE = "IIC - Adaptive IICPilot"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, eda_interface: EDAInterface, llm: LLMPort, memory: JSONMemory) -> None:
        super().__init__()
        self._eda_interface = eda_interface
        self._llm = llm
        self._memory = memory
        self._control: Optional[ControlAgent] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield RichLog(id="transcript", markup=True, wrap=True)
            yield Static(id="sidebar")
        yield Input(placeholder="Describe a design run, e.g. 'run gcd at 2.2ns clock'", id="request")
        yield Footer()

    def on_mount(self) -> None:
        self._control = ControlAgent(
            self._eda_interface,
            self._llm,
            self._memory,
            emit=self._on_emit,
            human_answer=self._human_answer,
        )
        self._refresh_sidebar()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.query_one("#transcript", RichLog).write(f"[bold]> {text}[/]")
        self.run_worker(lambda: self._run_session(text), thread=True)

    def _run_session(self, text: str) -> None:
        base = FlowConfig(design="gcd")
        result = self._control.handle_request(text, base)
        self.call_from_thread(self._on_result, result)

    def _on_result(self, result) -> None:
        self._refresh_sidebar(result)

    def _on_emit(self, line: str) -> None:
        self.call_from_thread(self.query_one("#transcript", RichLog).write, _colorize(line))

    def _human_answer(self, proposal: Proposal, config: FlowConfig, comment: str):
        answer_box: dict = {}
        done = threading.Event()

        def show_modal() -> None:
            def handle_dismiss(result: Optional[dict]) -> None:
                answer_box["result"] = result
                done.set()

            self.push_screen(HITLModal(proposal, config, comment), handle_dismiss)

        self.call_from_thread(show_modal)
        done.wait()

        result = answer_box.get("result")
        if result is None:
            return None
        return Proposal(
            changes=result["changes"],
            rationale=proposal.rationale,
            triggered_by=proposal.triggered_by,
            seeded_from_memory=proposal.seeded_from_memory,
        )

    def _refresh_sidebar(self, result=None) -> None:
        sidebar = self.query_one("#sidebar", Static)
        if result is None:
            sidebar.update("No runs yet.")
            return
        m = result.metrics
        sidebar.update(
            f"design: {result.config.design}\n"
            f"stage: {result.reached_stage}\n"
            f"ok: {result.ok}\n"
            f"wns: {m.wns_ns:.3f}ns\n"
            f"congestion: {m.congestion:.2f}\n"
            f"area: {m.area_um2:.1f}um2\n"
            f"power: {m.power_mw:.2f}mW"
        )
