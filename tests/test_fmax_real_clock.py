"""fmax / cp_delay come from the REAL clock's per-clock metadata key.

ORFS's headline finish__timing__fmax is the max over all clocks, virtual ones
included; for ibex that is the virtual I/O clock (591 MHz at 1.76 ns) while
core_clock's period_min gives 456.8 MHz. Fixtures are the finish__timing__*
keys of real proven runs and copies of the shipped SDCs, so these tests need no
ORFS install.
"""
import json
import unittest
from pathlib import Path
from unittest import mock

from adaptive_iicpilot.eda_interface import ORFSAdapter, real_clock_name, sdc_clocks
from adaptive_iicpilot.types import FlowConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fmax"


def _sdc(design: str) -> str:
    return (FIXTURES / f"{design}.sdc").read_text(encoding="utf-8")


def _adapter(sdc_by_design: dict) -> ORFSAdapter:
    """An adapter whose SDC reads come from fixtures, never from WSL."""
    adapter = ORFSAdapter()
    adapter._real_clocks = {}  # instance-level, so tests never share the class cache

    def fake_shell(command, timeout=None, on_line=None):
        for design, text in sdc_by_design.items():
            if f"/{design}/constraint.sdc" in command:
                return True, text
        return False, ""

    mock.patch.object(adapter, "_shell", side_effect=fake_shell).start()
    mock.patch.object(adapter, "_design_nickname", side_effect=lambda d: d).start()
    mock.patch.object(adapter, "timing_threshold_ns", return_value=0.0).start()
    return adapter


def _parse(design: str, run: str, clock: float, sdc_text=None):
    data = json.loads((FIXTURES / f"{run}.metadata.json").read_text(encoding="utf-8"))
    adapter = _adapter({design: sdc_text if sdc_text is not None else _sdc(design)})
    config = FlowConfig(design=design, core_utilization=80.0, clock_period_ns=clock)
    return adapter._parse_metadata(data, config)


class TestSdcClocks(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_real_clock_is_the_one_with_a_source(self):
        self.assertEqual(real_clock_name(_sdc("ibex")), "core_clock")
        self.assertEqual(real_clock_name(_sdc("aes")), "clk")
        self.assertEqual(real_clock_name(_sdc("gcd")), "core_clock")

    def test_virtual_clock_is_detected_and_its_name_resolved(self):
        virtual = [c for c in sdc_clocks(_sdc("ibex")) if c["virtual"]]
        self.assertEqual([c["name"] for c in virtual], ["vclk_core_clock"])

    def test_not_a_naming_heuristic(self):
        # A real clock named like a virtual one, and a virtual one named plainly.
        text = ("set p 1.0\ncreate_clock -name vclk_real -period $p [get_ports clk]\n"
                "create_clock -name io -period $p\n")
        self.assertEqual(real_clock_name(text), "vclk_real")

    def test_no_or_ambiguous_real_clock_gives_none(self):
        self.assertIsNone(real_clock_name("create_clock -name v -period 1\n"))
        self.assertIsNone(real_clock_name(
            "create_clock -name a -period 1 [get_ports a]\ncreate_clock -name b -period 1 [get_ports b]\n"))


class TestFmaxFromRealClock(unittest.TestCase):
    def tearDown(self):
        mock.patch.stopall()

    def test_ibex_1_76_uses_core_clock_not_the_virtual_clock(self):
        m = _parse("ibex", "ibex_1.76", 1.76)
        self.assertAlmostEqual(m.fmax_mhz, 456.8, places=1)
        self.assertEqual(m.fmax_clock, "core_clock")
        # cp_delay = clock - WNS when the WNS path is reg2reg.
        self.assertAlmostEqual(m.cp_delay_ns, 1.76 - m.wns_ns, places=4)
        # The headline is kept, labelled, and is the virtual clock.
        self.assertAlmostEqual(m.orfs_fmax_headline_hz / 1e6, 591.0, places=1)
        self.assertEqual(m.orfs_fmax_headline_clock, "vclk_core_clock")

    def test_aes_stock(self):
        m = _parse("aes", "aes_0.82", 0.82)
        self.assertAlmostEqual(m.fmax_mhz, 1199.6, places=1)
        self.assertEqual(m.fmax_clock, "clk")

    def test_gcd_stock(self):
        m = _parse("gcd", "gcd_0.25", 0.25)
        self.assertAlmostEqual(m.fmax_mhz, 2435.3, places=1)
        self.assertEqual(m.fmax_clock, "core_clock")

    def test_a_virtual_clock_is_never_used_for_fmax(self):
        # Real clock key missing: must report None, not fall back to the
        # (virtual-clock) headline or the virtual clock's own key.
        data = json.loads((FIXTURES / "ibex_1.76.metadata.json").read_text(encoding="utf-8"))
        del data["finish__timing__fmax__clock__core_clock"]
        adapter = _adapter({"ibex": _sdc("ibex")})
        m = adapter._parse_metadata(data, FlowConfig(design="ibex", core_utilization=80.0, clock_period_ns=1.76))
        self.assertIsNone(m.cp_delay_ns)
        self.assertIsNone(m.fmax_mhz)
        self.assertIn("core_clock", m.cp_delay_note)

    def test_unreadable_sdc_gives_none_never_the_headline(self):
        m = _parse("ibex", "ibex_1.76", 1.76, sdc_text="# no clocks\n")
        self.assertIsNone(m.cp_delay_ns)
        self.assertIsNone(m.fmax_mhz)
        self.assertIsNotNone(m.cp_delay_note)

    def test_verdict_says_reg_to_reg_fmax(self):
        from adaptive_iicpilot.chat import _verdict
        self.assertIn("reg-to-reg fmax 456.8 MHz", _verdict(_parse("ibex", "ibex_1.76", 1.76)))


if __name__ == "__main__":
    unittest.main()
