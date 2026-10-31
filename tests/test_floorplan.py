"""FloorplanAgent: legality checks, IO Tcl generation, SVG rendering,
variant isolation, config.mk classification, memory and the agent loop.

Everything here is offline: ORFS is replaced by a fake shell or fake runner.
"""
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from adaptive_iicpilot.floorplan import config_mk, defsvg, iotcl, macros
from adaptive_iicpilot.floorplan.agent import FloorplanAgent, score, usable
from adaptive_iicpilot.floorplan.knobs import FloorplanKnobs, Stage, knobs_from_dict, shell_quote_vars
from adaptive_iicpilot.floorplan.macros import Macro, Placement, Rect
from adaptive_iicpilot.floorplan.memory import FloorplanMemory
from adaptive_iicpilot.floorplan.runner import FloorplanRunner, VariantError

CORE = Rect(10, 10, 210, 160)
SRAM = Macro("u_sram0", "fakeram45_64x32", 20.14, 61.6)
SRAM2 = Macro("u_sram1", "fakeram45_64x32", 20.14, 61.6)


def P(name, x, y, w=20.0, h=60.0):
    return Placement(name, "m", x, y, w, h)


class TestLegality(unittest.TestCase):
    def test_legal_placement_has_no_violations(self):
        self.assertEqual(macros.check_legality([P("a", 20, 20), P("b", 80, 20)], CORE, (10, 10)), [])

    def test_outside_core_is_reported(self):
        problems = macros.check_legality([P("a", 200, 20)], CORE)
        self.assertEqual(len(problems), 1)
        self.assertIn("outside the core", problems[0])

    def test_overlap_is_reported(self):
        problems = macros.check_legality([P("a", 20, 20), P("b", 30, 30)], CORE)
        self.assertTrue(any("overlaps" in p for p in problems))

    def test_halo_intrusion_is_reported_without_overlap(self):
        # 5um apart: no overlap, but inside a 10um halo.
        problems = macros.check_legality([P("a", 20, 20), P("b", 45, 20)], CORE, (10, 10))
        self.assertEqual(len(problems), 1)
        self.assertIn("halo", problems[0])

    def test_gap_exactly_equal_to_halo_is_legal(self):
        self.assertEqual(macros.check_legality([P("a", 20, 20), P("b", 50, 20)], CORE, (10, 10)), [])

    def test_halo_is_per_axis(self):
        # 12um apart vertically: fine for a 10x5 halo, not for 5x15.
        a, b = P("a", 20, 20), P("b", 20, 92)
        self.assertEqual(macros.check_legality([a, b], CORE, (10, 5)), [])
        self.assertTrue(macros.check_legality([a, b], CORE, (5, 15)))

    def test_touching_macros_overlap_nothing_but_violate_any_halo(self):
        a, b = P("a", 20, 20), P("b", 40, 20)
        self.assertEqual(macros.check_legality([a, b], CORE), [])
        self.assertTrue(macros.check_legality([a, b], CORE, (1, 1)))


class TestMacroStrategies(unittest.TestCase):
    def test_every_strategy_produces_a_legal_placement(self):
        many = [Macro(f"m{i}", "fakeram", 20, 40) for i in range(6)]
        for strategy in ("periphery", "edge_cluster"):
            with self.subTest(strategy=strategy):
                placed = macros.generate(strategy, many, CORE, (5, 5))
                self.assertEqual(len(placed), 6)
                self.assertEqual(macros.check_legality(placed, CORE, (5, 5)), [])

    def test_periphery_puts_two_macros_on_opposite_edges(self):
        placed = {p.name: p for p in macros.periphery([SRAM, SRAM2], CORE, (10, 10))}
        ys = sorted(p.y for p in placed.values())
        self.assertLess(ys[0], CORE.y0 + 2)  # bottom edge
        self.assertGreater(ys[1] + 61.6, CORE.y1 - 2)  # top edge

    def test_edge_cluster_keeps_macros_on_one_edge(self):
        placed = macros.edge_cluster([SRAM, SRAM2], CORE, (10, 10))
        self.assertTrue(all(p.y < CORE.y0 + 2 for p in placed))

    def test_channel_widens_the_gap_beyond_the_halo(self):
        placed = sorted(macros.edge_cluster([SRAM, SRAM2], CORE, (5, 5), channel=30), key=lambda p: p.x)
        self.assertAlmostEqual(placed[1].x - placed[0].rect.x1, 30.0)

    def test_too_many_macros_raise_instead_of_returning_an_illegal_layout(self):
        huge = [Macro(f"m{i}", "big", 90, 70) for i in range(8)]
        with self.assertRaises(macros.PlacementError):
            macros.generate("edge_cluster", huge, CORE, (10, 10))

    def test_moves_apply_on_top_and_can_make_a_layout_illegal(self):
        placed = macros.generate("edge_cluster", [SRAM, SRAM2], CORE, (10, 10))
        moved = macros.apply_moves(placed, [(placed[1].name, placed[0].x + 5, placed[0].y)])
        self.assertTrue(macros.check_legality(moved, CORE, (10, 10)))

    def test_explicit_needs_every_macro(self):
        with self.assertRaises(macros.PlacementError):
            macros.generate("explicit", [SRAM, SRAM2], CORE, (0, 0), moves=[("u_sram0", 20, 20)])
        placed = macros.generate(
            "explicit", [SRAM, SRAM2], CORE, (0, 0), moves=[("u_sram0", 20, 20), ("u_sram1", 100, 20)]
        )
        self.assertEqual({(p.x, p.y) for p in placed}, {(20.0, 20.0), (100.0, 20.0)})

    def test_tcl_braces_hierarchical_names_and_logs_a_marker(self):
        tcl = macros.to_tcl([Placement("dcache/data.mem", "m", 1.5, 2.25, 1, 1)], "periphery")
        self.assertIn("place_macro -macro_name {dcache/data.mem} -location {1.500 2.250} -orientation R0", tcl)
        self.assertIn('puts "\\[Floorplan\\] MACRO_PLACEMENT_TCL strategy=periphery macros=1"', tcl)


PORTS = (
    [iotcl.Port(f"data_i[{i}]", "INPUT") for i in range(8)]
    + [iotcl.Port(f"data_o[{i}]", "OUTPUT") for i in range(4)]
    + [iotcl.Port("clk", "INPUT", "CLOCK"), iotcl.Port("rst", "INPUT"), iotcl.Port("done", "OUTPUT")]
    + [iotcl.Port("pad", "INOUT"), iotcl.Port("VDD", "INOUT", "POWER")]
)


class TestIoConstraints(unittest.TestCase):
    def test_default_strategy_writes_no_file(self):
        self.assertIsNone(iotcl.generate(PORTS, "default"))

    def test_buses_group_lsb_first_and_scalars_by_direction(self):
        groups = iotcl.groups_by_bus(PORTS)
        self.assertEqual([p.name for p in groups["data_o"]], [f"data_o[{i}]" for i in range(4)])
        self.assertEqual({p.name for p in groups["scalar_input"]}, {"clk", "rst"})
        self.assertNotIn("VDD", {p.name for m in groups.values() for p in m})

    def test_bus_indices_sort_numerically(self):
        ports = [iotcl.Port(f"a[{i}]", "INPUT") for i in (10, 2, 1)]
        self.assertEqual([p.name for p in iotcl.groups_by_bus(ports)["a"]], ["a[1]", "a[2]", "a[10]"])

    def test_inputs_left_outputs_right(self):
        tcl = iotcl.generate(PORTS, "in_left_out_right", "toy")
        self.assertIn("set_io_pin_constraint -region left:* -group -order -pin_names {data_i[0]", tcl)
        self.assertIn("set_io_pin_constraint -region right:* -group -order -pin_names {data_o[0]", tcl)
        self.assertIn("-region bottom:* -pin_names {pad}", tcl)  # single pin: no -group
        self.assertEqual(iotcl.side_summary(PORTS, "in_left_out_right"),
                         {"left": 10, "right": 5, "bottom": 1, "top": 0})

    def test_bus_sides_balances_and_keeps_each_bus_whole(self):
        ports = [iotcl.Port(f"b{k}[{i}]", "INPUT") for k in range(4) for i in range(16)]
        sides = iotcl.assign_sides(ports, "bus_sides")
        self.assertEqual(sorted(sides.values()), sorted(iotcl.SIDES))
        tcl = iotcl.generate(ports, "bus_sides")
        self.assertEqual(tcl.count("set_io_pin_constraint"), 4)

    def test_bus_sides_weights_sides_by_edge_length(self):
        ports = [iotcl.Port(f"b{k}[{i}]", "INPUT") for k in range(12) for i in range(8)]
        self.assertEqual(set(iotcl.side_summary(ports, "bus_sides").values()), {24})
        wide = iotcl.side_summary(ports, "bus_sides", aspect=0.5)
        self.assertEqual(wide["top"] + wide["bottom"], 2 * (wide["left"] + wide["right"]))
        self.assertTrue(all(v > 0 for v in wide.values()))
        self.assertEqual(FloorplanRunner.io_aspect(FloorplanKnobs(design="x", aspect_ratio=0.5)), 0.5)
        self.assertEqual(FloorplanRunner.io_aspect(
            FloorplanKnobs(design="x", die_area=(0, 0, 200, 100), core_area=(5, 5, 195, 95))), 0.5)

    def test_every_pin_is_constrained_exactly_once(self):
        for strategy in ("bus_sides", "in_left_out_right"):
            tcl = iotcl.generate(PORTS, strategy)
            names = [n for line in tcl.splitlines() if line.startswith("set_io")
                     for n in line.split("-pin_names {")[1].rstrip("}").split()]
            self.assertEqual(sorted(names), sorted(p.name for p in iotcl.signal_ports(PORTS)))

    def test_unknown_strategy_is_rejected(self):
        with self.assertRaises(ValueError):
            iotcl.generate(PORTS, "diagonal")


DEF_TEXT = """VERSION 5.8 ;
DESIGN toy ;
UNITS DISTANCE MICRONS 2000 ;
DIEAREA ( 0 0 ) ( 440000 340000 ) ;
ROW ROW_0 FreePDK45_38x28_10R_NP_162NW_34O 20140 22400 FS DO 1000 BY 1 STEP 380 0 ;
ROW ROW_1 FreePDK45_38x28_10R_NP_162NW_34O 20140 25200 N DO 1000 BY 1 STEP 380 0 ;
COMPONENTS 3 ;
    - dcache/data\\.mem fakeram45_64x32 + FIXED ( 40000 40000 ) N ;
    - u_rot fakeram45_64x32 + PLACED ( 200000 40000 ) E ;
    - _123_ INV_X1 + PLACED ( 100 100 ) N ;
END COMPONENTS
PINS 3 ;
    - clk + NET clk + DIRECTION INPUT + USE SIGNAL
      + PORT
        + LAYER metal6 ( -70 -70 ) ( 70 70 )
        + PLACED ( 0 170000 ) N ;
    - out\\[3\\] + NET out[3] + DIRECTION OUTPUT + USE SIGNAL + LAYER metal5 ( -70 -70 ) ( 70 70 ) + PLACED ( 440000 9000 ) W ;
    - VDD + NET VDD + SPECIAL + DIRECTION INOUT + USE POWER ;
END PINS
END DESIGN
"""


class TestDefAndSvg(unittest.TestCase):
    def setUp(self):
        self.layout = defsvg.parse_def(DEF_TEXT, macro_masters={"fakeram45_64x32"})

    def test_units_die_and_rows(self):
        self.assertEqual(self.layout.dbu, 2000.0)
        self.assertEqual(self.layout.die, Rect(0, 0, 220, 170))
        core = self.layout.core_from_rows()
        self.assertAlmostEqual(core.x0, 10.07)
        self.assertAlmostEqual(core.y1, 12.6 + 1.4)

    def test_only_macros_are_kept_and_names_unescaped(self):
        self.assertEqual([c.name for c in self.layout.components], ["dcache/data.mem", "u_rot"])

    def test_pins_both_syntaxes_power_skipped(self):
        pins = {p.name: p for p in self.layout.pins}
        self.assertEqual(set(pins), {"clk", "out[3]"})
        self.assertEqual((pins["clk"].x, pins["clk"].y, pins["clk"].direction), (0.0, 85.0, "INPUT"))
        self.assertEqual(pins["out[3]"].layer, "metal5")

    def test_rotated_macro_swaps_width_and_height(self):
        rects = {name: r for name, _m, r in defsvg.macro_rects(self.layout, {"fakeram45_64x32": (20.14, 61.6)})}
        self.assertAlmostEqual(rects["u_rot"].width, 61.6)
        self.assertAlmostEqual(rects["dcache/data.mem"].width, 20.14)

    def test_svg_is_valid_and_draws_everything(self):
        svg = defsvg.render_svg(
            self.layout, core=Rect(10, 10, 210, 160), macro_sizes={"fakeram45_64x32": (20.14, 61.6)},
            halo=(10, 10), pin_groups={"clk": "scalar_input", "out[3]": "out"},
            title="toy <AR=1>", subtitle="test",
        )
        root = ET.fromstring(svg)  # well-formed XML, title escaped
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("toy &lt;AR=1&gt;", svg)
        self.assertEqual(svg.count('stroke-dasharray="3 2"'), 2)  # one halo per macro
        self.assertIn("out[3] (out, output)", svg)
        self.assertNotIn("<script", svg)
        colors = {e.get("fill") for e in root.iter("{http://www.w3.org/2000/svg}rect")
                  if e.find("{http://www.w3.org/2000/svg}title") is not None}
        self.assertEqual(len(colors), 2)  # two pin groups, two colors

    def test_short_wide_die_still_fits_the_legend(self):
        wide = defsvg.DefLayout(dbu=1000, die=Rect(0, 0, 400, 50))
        root = ET.fromstring(defsvg.render_svg(wide))
        self.assertGreaterEqual(float(root.get("height")), 14 + len(defsvg.PALETTE) * 13 + 70)


TINYROCKET_MK = """export DESIGN_NICKNAME = tinyRocket
export ADDITIONAL_LEFS = $(sort $(wildcard $(DESIGN_HOME)/$(PLATFORM)/$(DESIGN_NICKNAME)/*.lef))
export CORE_UTILIZATION       = 60
export CORE_ASPECT_RATIO      = 1
export CORE_MARGIN            = 2
export PLACE_DENSITY          = 0.75
export SYNTH_MINIMUM_KEEP_SIZE ?= 5000
"""


class TestConfigMk(unittest.TestCase):
    def test_plain_assignments_need_the_command_line(self):
        report = config_mk.classify(TINYROCKET_MK)
        hard = report["command_line_required"]
        self.assertEqual(hard["CORE_UTILIZATION"], {"op": "=", "value": "60"})
        self.assertIn("CORE_ASPECT_RATIO", hard)
        self.assertIn("IO_CONSTRAINTS", report["not_set_by_design"])

    def test_soft_assignments_and_continuations(self):
        text = "export CORE_UTILIZATION ?= 50 # comment\nexport DIE_AREA = 0 0 \\\n   800 600\n"
        report = config_mk.classify(text)
        self.assertEqual(report["env_overridable"], {"CORE_UTILIZATION": "50"})
        self.assertEqual(report["command_line_required"]["DIE_AREA"]["value"], "0 0 800 600")


class TestKnobsAndVariants(unittest.TestCase):
    def test_variant_is_deterministic_prefixed_and_stage_specific(self):
        k = FloorplanKnobs(design="ibex", aspect_ratio=0.5)
        self.assertEqual(k.variant(Stage.SCREEN), FloorplanKnobs(design="ibex", aspect_ratio=0.5).variant(Stage.SCREEN))
        self.assertTrue(k.variant(Stage.SCREEN).startswith("fp_screen_"))
        self.assertTrue(k.variant(Stage.FULL).startswith("fp_full_"))
        self.assertNotEqual(k.variant(Stage.SCREEN), k.variant(Stage.FULL))

    def test_every_knob_changes_the_variant(self):
        base = FloorplanKnobs(design="tinyRocket", macro_mode="auto")
        variants = {base.variant(Stage.SCREEN)}
        for change in (
            {"design": "ibex"}, {"aspect_ratio": 1.5}, {"core_margin": 4.0}, {"core_utilization": 40.0},
            {"io_strategy": "bus_sides"}, {"macro_halo": (5.0, 5.0)},
            {"macro_mode": "manual", "macro_strategy": "periphery"},
            {"macro_mode": "manual", "macro_strategy": "periphery", "macro_channel": 8.0},
            {"die_area": (0, 0, 300, 300), "core_area": (10, 10, 290, 290)},
            {"macro_mode": "manual", "macro_strategy": "explicit", "macro_moves": (("a", 1.0, 2.0),)},
        ):
            variants.add(base.tweaked(**change).variant(Stage.SCREEN))
        self.assertEqual(len(variants), 11)

    def test_make_vars_area_mode_blanks_utilization(self):
        k = FloorplanKnobs(design="ibex", die_area=(0, 0, 200, 200), core_area=(5, 5, 195, 195))
        mv = k.make_vars()
        self.assertEqual(mv["DIE_AREA"], "0 0 200 200")
        self.assertEqual(mv["CORE_UTILIZATION"], "")

    def test_make_vars_util_mode_blanks_areas(self):
        mv = FloorplanKnobs(design="bp_fe_top", core_utilization=40, aspect_ratio=2,
                            macro_mode="auto", macro_halo=(10, 5)).make_vars(io_tcl="/x/io.tcl")
        self.assertEqual((mv["DIE_AREA"], mv["CORE_AREA"], mv["CORE_UTILIZATION"]), ("", "", "40"))
        self.assertEqual(mv["MACRO_PLACE_HALO"], "10 5")
        self.assertEqual(mv["IO_CONSTRAINTS"], "/x/io.tcl")
        self.assertEqual(shell_quote_vars({"A": "10 5", "B": ""}), "A='10 5' B=''")

    def test_invalid_combinations_are_rejected(self):
        with self.assertRaises(ValueError):
            FloorplanKnobs(design="x", die_area=(0, 0, 1, 1))
        with self.assertRaises(ValueError):
            FloorplanKnobs(design="x", die_area=(0, 0, 1, 1), core_area=(0, 0, 1, 1), core_utilization=50)
        with self.assertRaises(ValueError):
            FloorplanKnobs(design="x", macro_mode="manual")

    def test_round_trip_through_dict(self):
        k = FloorplanKnobs(design="t", macro_mode="manual", macro_strategy="explicit",
                           macro_halo=(5.0, 5.0), macro_moves=(("a", 1.0, 2.0),))
        self.assertEqual(knobs_from_dict(json.loads(json.dumps(k.as_dict()))), k)


class FakeShell:
    """Stands in for ORFSAdapter._shell; records every command."""

    def __init__(self, busy=False):
        self.commands = []
        self.busy = busy

    def __call__(self, command, timeout=None, on_line=None):
        self.commands.append(command)
        if "DESIGN_NICKNAME" in command:
            return True, ""
        if self.busy and "flock -n" in command:
            return False, "FP_VARIANT_BUSY"
        if "clean_all" in command or "cp -a" in command:
            return True, ""
        return False, "stop here"


class TestVariantIsolation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runner = FloorplanRunner(orfs_root="/orfs", artifacts_dir=Path(self.tmp.name), emit=lambda s: None)
        self.shell = FakeShell()
        self.runner.orfs._shell = self.shell

    def tearDown(self):
        self.tmp.cleanup()

    def test_base_and_malformed_variants_are_refused(self):
        for bad in ("base", "", "fp_../base", "screen_123", "fp_x/../../base"):
            with self.subTest(variant=bad), self.assertRaises(VariantError):
                self.runner.make_command("ibex", bad, {}, "clean_all")

    def test_make_command_pins_the_variant_and_takes_a_variant_lock(self):
        k = FloorplanKnobs(design="ibex", aspect_ratio=2.0)
        v = k.variant(Stage.SCREEN)
        cmd = self.runner.make_command(k, v, k.make_vars(), "clean_all")
        self.assertIn(f"FLOW_VARIANT={v} ", cmd)
        self.assertIn(f"flock -n -E 75 /tmp/adaptive_iicpilot_fp_nangate45_ibex_{v}.lock make", cmd)
        self.assertIn("CORE_ASPECT_RATIO='2'", cmd)

    def test_variant_dirs_never_point_at_base(self):
        dirs = self.runner.variant_dirs("ibex", "fp_screen_abc")
        self.assertEqual(dirs["results"], "results/nangate45/ibex/fp_screen_abc")
        self.assertTrue(all(not d.endswith("/base") for d in dirs.values()))

    def test_a_run_only_ever_names_its_own_variant(self):
        k = FloorplanKnobs(design="ibex", aspect_ratio=2.0, io_strategy="in_left_out_right")
        info = {"dbu": 2000, "pins": [{"name": "a[0]", "direction": "INPUT"}], "macros": []}
        with mock.patch.object(self.runner, "synth_variant", return_value="fp_synth_x"):
            self.runner.run(k, Stage.SCREEN, synth_info=info, force=True)
        makes = [c for c in self.shell.commands if " make " in c]
        self.assertTrue(makes)
        for command in makes:
            self.assertRegex(command, r"FLOW_VARIANT=fp_(screen|synth)_\w+")
            self.assertNotIn("FLOW_VARIANT=base", command)
        clean = [c for c in makes if "clean_all" in c]
        self.assertEqual(len(clean), 1)
        self.assertIn(f"FLOW_VARIANT={k.variant(Stage.SCREEN)} ", clean[0])
        for command in self.shell.commands:
            self.assertNotRegex(command, r"/base\b")

    def test_measured_proof_compares_geometry_with_the_request(self):
        k = FloorplanKnobs(design="gcd", aspect_ratio=0.5, core_margin=4)
        lines = FloorplanRunner.measured_proof(k, {"die_rect": [0, 0, 56, 32], "core_rect": [4, 4, 52, 28]})
        self.assertIn("geometry: measured core aspect (h/w) 0.500 for CORE_ASPECT_RATIO 0.5", lines)
        self.assertIn("geometry: measured die-to-core gaps 4.00-4.00um for CORE_MARGIN 4", lines)
        area = FloorplanKnobs(design="gcd", die_area=(0, 0, 60, 60), core_area=(6, 6, 54, 54))
        self.assertEqual(FloorplanRunner.measured_proof(area, {"die_rect": [0, 0, 60, 60], "core_rect": [6, 6, 54, 54]}),
                         ["geometry: measured die (0,0)-(60,60) for DIE_AREA 0 0 60 60"])

    def test_a_busy_variant_is_never_run_twice(self):
        self.runner.orfs._shell = FakeShell(busy=True)
        k = FloorplanKnobs(design="ibex")
        with self.assertRaises(VariantError):
            self.runner.run(k, Stage.SCREEN, synth_info={"pins": [], "macros": []}, force=True)

    def test_illegal_manual_placement_stops_before_the_flow(self):
        k = FloorplanKnobs(design="tinyRocket", macro_mode="manual", macro_strategy="explicit",
                           macro_halo=(10.0, 10.0),
                           macro_moves=(("m0", 20.0, 20.0), ("m1", 25.0, 20.0)))
        probe = {"core": [10, 10, 210, 160], "macros": [
            {"name": "m0", "master": "f", "width": 20, "height": 60},
            {"name": "m1", "master": "f", "width": 20, "height": 60}]}
        self.runner.orfs._shell = lambda command, timeout=None, on_line=None: (True, "")
        seen = []
        orig = self.runner._run_make
        with mock.patch.object(self.runner, "probe", return_value=probe), \
                mock.patch.object(self.runner, "seed_synth"), \
                mock.patch.object(self.runner, "_run_make", side_effect=lambda c, l: (seen.append(c), (True, ""))[1]):
            result = self.runner.run(k, Stage.SCREEN, synth_info={"pins": [], "macros": []}, force=True)
        self.assertFalse(result["legal"])
        self.assertTrue(any("overlaps" in v for v in result["violations"]))
        # clean_all and the 2_1_floorplan step only; the flow itself never ran.
        self.assertEqual(len(seen), 2)
        self.assertIn("2_1_floorplan.odb", seen[1])
        self.assertFalse((Path(self.tmp.name) / "tinyRocket" / k.variant(Stage.SCREEN) / "macros.tcl").exists())
        del orig


def fake_result(knobs, stage, congestion, io=100.0, wl=1000.0, die=5000.0, wns=None, ok=True,
                legal=None, svg=None, macros_=None):
    return {
        "design": knobs.design, "tech": "nangate45", "knobs": knobs.as_dict(), "label": knobs.label(),
        "stage": stage, "variant": knobs.variant(stage), "ok": ok, "legal": legal, "source": "REAL_ORFS",
        "metrics": {"congestion": congestion, "io_hpwl_um": io, "gp_wirelength_um": wl,
                    "die_area_um2": die, "wns_ns": wns},
        "elapsed_s": 1.0, "svg": svg, "macros": macros_ or [],
    }


class FakeRunner:
    def __init__(self, table, has_macros=False):
        self.table = table  # label -> congestion
        self.has_macros = has_macros
        self.ran = []

    def config_report(self, design, tech="nangate45"):
        return {"command_line_required": {}, "env_overridable": {}, "not_set_by_design": []}

    def active_flows(self):
        return [{"design": "ibex", "variant": "base", "pid": "1"}]

    def supports_variable(self, name):
        return False

    def ensure_synth(self, design, tech="nangate45"):
        return {"macros": [{"name": "m0"}] if self.has_macros else [], "pins": []}

    def cached(self, knobs, stage):
        return None

    def run(self, knobs, stage, synth_info=None, force=False):
        self.ran.append((knobs, stage))
        congestion = self.table.get(knobs.label(), 0.99)
        wns = -0.01 if stage == Stage.FULL else None
        macros_ = [{"name": "m0", "x": 20.0, "y": 20.0}] if knobs.macro_mode != "none" else []
        return fake_result(knobs, stage, congestion, wns=wns, macros_=macros_, svg=self.svg)

    svg = None


class TestAgent(unittest.TestCase):
    def setUp(self):
        self.memory = FloorplanMemory(":memory:")
        self.lines = []

    def agent(self, runner, **kw):
        return FloorplanAgent(runner=runner, memory=self.memory, emit=self.lines.append, **kw)

    def test_ranking_gates_congestion_and_failures(self):
        k = FloorplanKnobs(design="x")
        pool = [
            fake_result(k.tweaked(aspect_ratio=1.0), "SCREEN", 0.9),
            fake_result(k.tweaked(aspect_ratio=2.0), "SCREEN", 1.2, io=10, wl=10),
            fake_result(k.tweaked(aspect_ratio=0.5), "SCREEN", 0.8, ok=False),
            fake_result(k.tweaked(aspect_ratio=1.5), "SCREEN", 0.85, legal=False),
        ]
        ordered = sorted(pool, key=lambda r: score(r, "balanced", pool))
        self.assertEqual(ordered[0]["label"], "AR=1")
        self.assertEqual(ordered[1]["label"], "AR=2")  # congested, but usable
        self.assertFalse(usable(ordered[2]) or usable(ordered[3]))

    def test_a_full_run_ranks_ahead_of_its_identical_screen(self):
        from adaptive_iicpilot.floorplan.agent import ranked

        k = FloorplanKnobs(design="x", aspect_ratio=1.0)
        pool = [fake_result(k, "SCREEN", 0.9), fake_result(k, "FULL", 0.9, wns=-0.1)]
        self.assertEqual(ranked(pool, "balanced")[0]["stage"], "FULL")

    def test_ibex_study_screens_shape_and_pins_and_validates_finals(self):
        runner = FakeRunner({"AR=0.5": 0.95, "AR=1": 0.97, "AR=2": 1.01})
        report = self.agent(runner).run("ibex", "congestion", ["aspect", "pins"], full="per_family")
        screened = [r for r in report["table"] if r["kind"] == "SCREEN"]
        self.assertEqual(len(screened), 7)  # 5 aspect + 3 pins, AR=1/default shared
        self.assertEqual({r["family"] for r in screened}, {"shape", "pins"})
        self.assertEqual(len(report["screen_vs_full"]), 2)
        self.assertTrue(all(c["match"] for c in report["screen_vs_full"]))
        self.assertEqual(len(self.memory.runs("ibex", "SCREEN")), 7)
        self.assertEqual(len(self.memory.runs("ibex", "FULL")), 2)
        self.assertEqual(self.memory.best("ibex", "congestion")[0]["label"], "AR=0.5")
        self.assertTrue(any("ibex/base" in line for line in self.lines))

    def test_rejected_finalist_gets_no_full_run(self):
        runner = FakeRunner({})
        agent = self.agent(runner, decide=lambda proposal, config: None)
        report = agent.run("ibex", "balanced", ["margin"], full="best")
        self.assertFalse([s for _k, s in runner.ran if s == Stage.FULL])
        self.assertEqual(report["screen_vs_full"], [])

    def test_hitl_card_carries_the_image_and_a_macro_move_becomes_a_correction(self):
        with tempfile.TemporaryDirectory() as tmp:
            svg = Path(tmp) / "fp.svg"
            svg.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
            runner = FakeRunner({}, has_macros=True)
            runner.svg = str(svg)
            seen = {}

            def decide(proposal, config):
                seen["proposal"] = proposal
                changes = dict(proposal.changes)
                changes["m0.x"] = 55.0
                return type(proposal)(changes=changes, rationale=proposal.rationale,
                                      triggered_by=proposal.triggered_by)

            images = []
            agent = self.agent(runner, decide=decide, on_image=images.append)
            agent.run("tinyRocket", "congestion", ["macros_auto"], full="best")

        self.assertIn("<svg", seen["proposal"].image_svg)
        self.assertEqual(seen["proposal"].changes["m0.x"], 20.0)
        corrections = self.memory.corrections("tinyRocket", "macro_move")
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["final"], [55.0, 20.0])
        finals = [k for k, s in runner.ran if s == Stage.FULL]
        self.assertEqual(finals[0].macro_strategy, "explicit")
        self.assertEqual(finals[0].macro_moves, (("m0", 55.0, 20.0),))
        # The edited finalist is screened too, so it has both numbers.
        self.assertTrue(any(k == finals[0] and s == Stage.SCREEN for k, s in runner.ran))
        self.assertTrue(images and "<svg" in images[0]["svg"])
        # Next study re-proposes the engineer's move.
        cands = self.agent(FakeRunner({}, True)).candidates("tinyRocket", ["macros_manual"], True)
        self.assertTrue(any(c.macro_strategy == "explicit" for c in cands))

    def test_not_run_when_runs_are_not_allowed(self):
        runner = FakeRunner({})
        report = self.agent(runner, allow_runs=False).run("ibex", "balanced", ["pins"], full="best")
        self.assertEqual(runner.ran, [])
        self.assertTrue(all(r["source"] == "NOT_RUN" for r in report["table"]))
        self.assertIsNone(report["best"])


class TestUtilScopeAndCleanFinals(unittest.TestCase):
    def setUp(self):
        self.memory = FloorplanMemory(":memory:")

    def test_a_congested_small_die_never_beats_a_clean_one(self):
        from adaptive_iicpilot.floorplan.agent import ranked

        k = FloorplanKnobs(design="ibex")
        pool = [fake_result(k.tweaked(core_utilization=90.0, aspect_ratio=0.75), "SCREEN", 1.1355, die=31998.1, wl=148552),
                fake_result(k.tweaked(core_utilization=85.0, aspect_ratio=0.5), "SCREEN", 0.9822, die=33895.8, wl=156657)]
        for objective in ("area", "wirelength", "io_hpwl", "balanced", "congestion"):
            with self.subTest(objective=objective):
                self.assertEqual(ranked(pool, objective)[0]["knobs"]["core_utilization"], 85.0)

    def test_scope_names(self):
        from adaptive_iicpilot.floorplan.agent import study_scope

        self.assertEqual(FloorplanKnobs(design="ibex", aspect_ratio=0.5).scope, "shape-only")
        self.assertEqual(FloorplanKnobs(design="ibex", io_strategy="bus_sides").scope, "pin strategy")
        self.assertEqual(study_scope(["aspect", "util"]), "shape-only")
        self.assertEqual(study_scope(["aspect", "pins"]), "pins+shape")

    def test_utils_multiply_candidates_and_aspects_replace_the_sweep(self):
        agent = FloorplanAgent(runner=FakeRunner({}), memory=self.memory, emit=lambda s: None)
        cands = agent.candidates("ibex", ["aspect"], False, utils=[85, 90], aspects=[0.5, 0.75])
        self.assertEqual(sorted((k.aspect_ratio, k.core_utilization) for k in cands),
                         [(0.5, 85.0), (0.5, 90.0), (0.75, 85.0), (0.75, 90.0)])
        only_util = agent.candidates("ibex", ["util"], False, utils=[80, 85])
        self.assertEqual([(k.core_utilization, k.aspect_ratio) for k in only_util], [(80.0, None), (85.0, None)])
        with self.assertRaises(ValueError):
            agent.candidates("ibex", ["util"], False)

    def test_best_clean_skips_the_full_run_when_everything_congests(self):
        runner = FakeRunner({"util=85 AR=0.5": 1.02, "util=85 AR=0.75": 1.03})
        lines = []
        agent = FloorplanAgent(runner=runner, memory=self.memory, emit=lines.append)
        agent.run("ibex", "area", ["aspect"], full="best_clean", utils=[85], aspects=[0.5, 0.75])
        self.assertFalse([s for _k, s in runner.ran if s == Stage.FULL])
        self.assertTrue(any("no full run" in line for line in lines))

    def test_best_clean_runs_the_clean_winner(self):
        runner = FakeRunner({"util=90 AR=0.5": 1.04, "util=85 AR=0.5": 0.99})
        agent = FloorplanAgent(runner=runner, memory=self.memory, emit=lambda s: None)
        report = agent.run("ibex", "area", ["aspect"], full="best_clean", utils=[85, 90], aspects=[0.5])
        finals = [k for k, s in runner.ran if s == Stage.FULL]
        self.assertEqual([(k.core_utilization, k.aspect_ratio) for k in finals], [(85.0, 0.5)])
        self.assertIn("util 85/90%, shape-only", report["scope"])

    def test_cache_checks_compare_with_the_dse_full_run(self):
        from adaptive_iicpilot.types import Metrics, RunResult, FlowConfig as FC

        runner = FakeRunner({})
        runner.stock_util = lambda design, tech="nangate45": 50.0
        stored = {}

        class Orfs:
            def effective_clock_ns(self, config):
                return 2.2

            def is_cached(self, config):
                stored["config"] = config
                return config.core_utilization == 80.0

            def _load_cached(self, config):
                m = Metrics(stage="finish", wns_ns=0, tns_ns=0, congestion=0.973745, cell_count=1,
                            area_um2=1, power_mw=1)
                return RunResult(config=config, reached_stage="finish", metrics=m)

        runner.orfs = Orfs()
        agent = FloorplanAgent(runner=runner, memory=self.memory, emit=lambda s: None)
        k = FloorplanKnobs(design="ibex", core_utilization=80.0)
        screened = [fake_result(k, "SCREEN", 0.973745),
                    fake_result(k.tweaked(core_utilization=85.0), "SCREEN", 1.01792),
                    fake_result(k.tweaked(io_strategy="bus_sides"), "SCREEN", 0.9)]
        checks = agent.cache_checks(screened)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0]["match"])
        self.assertEqual((stored["config"].placement_density, stored["config"].clock_period_ns), (0.0, 2.2))


class TestPipelineFloorplanStage(unittest.TestCase):
    """The FloorplanAgent as the floorplan step of the optimize pipeline."""

    def setUp(self):
        self.memory = FloorplanMemory(":memory:")
        stock = FloorplanKnobs(design="ibex")
        rows = [
            (stock.tweaked(aspect_ratio=1.0), 0.97), (stock.tweaked(aspect_ratio=0.5), 0.93),
            (stock.tweaked(aspect_ratio=2.0), 1.03), (stock.tweaked(aspect_ratio=1.0, io_strategy="bus_sides"), 0.95),
            # Higher util has a lower die area but must not win: DSE owns util.
            (stock.tweaked(core_utilization=85.0, aspect_ratio=0.5), 0.98),
        ]
        for knobs, cong in rows:
            self.memory.record(fake_result(knobs, "SCREEN", cong, die=33000 if knobs.core_utilization else 57000))
        tiny = FloorplanKnobs(design="tinyRocket", macro_mode="auto")
        self.memory.record(fake_result(tiny.tweaked(macro_halo=(10.0, 10.0)), "SCREEN", 0.97))
        self.memory.record(fake_result(tiny, "SCREEN", 1.02))
        self.memory.record(fake_result(tiny.tweaked(macro_mode="manual", macro_strategy="periphery"), "SCREEN", 0.90))
        self.lines = []
        self.patch = mock.patch("adaptive_iicpilot.floorplan.runner.FloorplanRunner.stock_util", return_value=50.0)
        self.patch.start()
        self.env = mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_BACKEND": "orfs"})
        self.env.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()

    def stage(self, base, **kw):
        from adaptive_iicpilot.floorplan.agent import floorplan_stage

        return floorplan_stage(base, self.memory, self.lines.append, **kw)

    def test_choice_comes_from_the_common_utilization_and_applies(self):
        from adaptive_iicpilot.types import FlowConfig as FC

        cfg, report = self.stage(FC(design="ibex", core_utilization=50.0, placement_density=0.0))
        self.assertTrue(report["applied"])
        self.assertEqual((cfg.aspect_ratio, cfg.floorplan["io_strategy"]), (0.5, "default"))
        self.assertEqual(cfg.core_utilization, 50.0)  # util untouched
        self.assertIn("measured at util 50%", report["choice"]["basis"])

    def test_manual_macro_placements_are_skipped_and_halo_carried(self):
        from adaptive_iicpilot.types import FlowConfig as FC

        cfg, report = self.stage(FC(design="tinyRocket"))
        self.assertEqual(cfg.floorplan["macro_halo"], [10.0, 10.0])
        self.assertEqual(report["choice"]["skipped_manual"], ["manual=periphery"])

    def test_apply_if_cached_keeps_stock_when_the_ladder_is_unmeasured(self):
        from adaptive_iicpilot.types import FlowConfig as FC

        base = FC(design="ibex")
        cfg, report = self.stage(base, policy="apply_if_cached", is_measured=lambda c: False)
        self.assertIs(cfg, base)
        self.assertFalse(report["applied"])
        seen = []
        cfg, report = self.stage(base, policy="apply_if_cached", is_measured=lambda c: seen.append(c) or True)
        self.assertTrue(report["applied"])
        self.assertEqual(seen[0].aspect_ratio, 0.5)

    def test_mock_backend_stock_policy_explicit_request_and_empty_memory_are_left_alone(self):
        from adaptive_iicpilot.types import FlowConfig as FC

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_BACKEND": "mock"}):
            self.assertEqual(self.stage(FC(design="ibex"))[1]["reason"], "mock backend")
        self.assertEqual(self.stage(FC(design="ibex"), policy="stock")[1]["reason"], "stock requested")
        self.assertEqual(self.stage(FC(design="ibex", aspect_ratio=1.5))[1]["reason"], "set by request")
        self.assertEqual(self.stage(FC(design="aes"))[1]["reason"], "no floorplan study in memory")

    def test_dse_keeps_the_staged_floorplan_over_memorys_best_shape(self):
        from adaptive_iicpilot.dse_agent import DSEAgent
        from adaptive_iicpilot.types import FlowConfig as FC, MemorySeed

        staged = FC(design="ibex", aspect_ratio=0.5, floorplan={"io_strategy": "default"})
        seed = MemorySeed(best_config=FC(design="ibex", core_utilization=80.0, aspect_ratio=1.0), best_score=1.0)
        cfg, warm = DSEAgent(eda_agent=None).starting_config(staged, seed)
        self.assertTrue(warm)
        self.assertEqual((cfg.core_utilization, cfg.aspect_ratio), (80.0, 0.5))
        self.assertEqual(cfg.floorplan, {"io_strategy": "default"})

    def test_adapter_keys_and_overrides(self):
        from adaptive_iicpilot.eda_interface import ORFSAdapter
        from adaptive_iicpilot.types import FlowConfig as FC

        adapter = ORFSAdapter(orfs_root="/orfs")
        adapter._stock_clocks["ibex"] = 2.2
        stock = FC(design="ibex", core_utilization=80.0, placement_density=0.0)
        shaped = stock.tweaked(aspect_ratio=0.5, floorplan={"label": "a", "io_strategy": "default"})
        pinned = shaped.tweaked(floorplan={"label": "b", "io_strategy": "bus_sides"})
        relabelled = shaped.tweaked(floorplan={"label": "zzz", "io_strategy": "default"})
        self.assertEqual(adapter.cache_key(shaped), adapter.cache_key(relabelled))
        self.assertNotEqual(adapter.cache_key(shaped), adapter.cache_key(pinned))
        self.assertEqual(adapter._floorplan_overrides(stock), "")
        halo = stock.tweaked(floorplan={"core_margin": 4.0, "macro_halo": [10.0, 5.0]})
        self.assertEqual(adapter._floorplan_overrides(halo),
                         "CORE_ASPECT_RATIO=1 CORE_MARGIN=4 MACRO_PLACE_HALO='10 5'")
        with mock.patch.object(adapter, "_io_constraints_file", return_value="/x/io.tcl"):
            self.assertIn("IO_CONSTRAINTS=/x/io.tcl", adapter._floorplan_overrides(pinned))

    def test_orchestrator_reports_the_stage(self):
        from adaptive_iicpilot.web.compat import InMemoryMemory, Orchestrator, Task

        lines = []
        iface = mock.Mock(emit=lines.append, decide=lambda p, c: p)
        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_BACKEND": "mock"}):
            report = Orchestrator(memory=InMemoryMemory(), interface=iface).run(Task(design="ibex", max_iters=1))
        self.assertEqual(report.floorplan["reason"], "mock backend")
        self.assertTrue(any(line.startswith("[Floorplan] stage skipped") for line in lines))


class TestFloorplanMemory(unittest.TestCase):
    def test_keyed_by_all_knobs_and_kind(self):
        memory = FloorplanMemory(":memory:")
        k = FloorplanKnobs(design="ibex", aspect_ratio=0.5)
        memory.record(fake_result(k, "SCREEN", 0.9))
        memory.record(fake_result(k, "SCREEN", 0.91))  # re-measurement replaces
        memory.record(fake_result(k, "FULL", 0.91, wns=-0.02))
        memory.record(fake_result(k.tweaked(io_strategy="bus_sides"), "SCREEN", 0.95))
        self.assertEqual(len(memory.runs("ibex")), 3)
        self.assertEqual(memory.lookup(k.as_dict(), "SCREEN")["congestion"], 0.91)
        self.assertEqual(memory.lookup(k.as_dict(), "FULL")["wns_ns"], -0.02)
        self.assertIsNone(memory.lookup(k.tweaked(aspect_ratio=2.0).as_dict(), "SCREEN"))

    def test_latest_move_per_macro_wins(self):
        memory = FloorplanMemory(":memory:")
        memory.record_correction("t", "macro_move", "m0", [1, 1], [5, 5])
        memory.record_correction("t", "macro_move", "m0", [5, 5], [7, 8])
        memory.record_correction("t", "knob", "aspect_ratio", 1.0, 1.5)
        self.assertEqual(memory.latest_macro_moves("t"), {"m0": (7, 8)})


class TestChatIntegration(unittest.TestCase):
    def test_tool_is_offered_and_floorplan_lines_are_tagged(self):
        from adaptive_iicpilot.chat import TOOLS, _split_log_line

        self.assertIn("floorplan", [t["function"]["name"] for t in TOOLS])
        self.assertEqual(_split_log_line("[Floorplan] proof x"), ("Floorplan", "proof x"))

    def test_session_tool_returns_a_table_without_svg_text(self):
        import os
        from adaptive_iicpilot.chat import Session

        report = {
            "studies": ["pins"], "config_mk": {"command_line_required": {"CORE_UTILIZATION": {}}},
            "table": [{"rank": 1, "label": "AR=1", "source": "CACHED", "svg": "/x.svg",
                       "variant": "fp_screen_1", "congestion": 0.9, "io_hpwl_um": 5.0,
                       "gp_wirelength_um": 9.0, "wns_ns": None}],
            "best": {"rank": 1, "label": "AR=1", "congestion": 0.9, "io_hpwl_um": 5.0,
                     "gp_wirelength_um": 9.0, "wns_ns": None, "svg": "/x.svg"},
            "best_svg_path": "/x.svg", "screen_vs_full": [],
        }
        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            session = Session(emit=lambda s: None)
        with mock.patch("adaptive_iicpilot.floorplan.agent.FloorplanAgent.run", return_value=report) as run:
            result = session.dispatch("floorplan", {"design": "ibex", "strategies": "pins,aspect"})
        self.assertEqual(run.call_args.args[2], ["pins", "aspect"])
        self.assertEqual(result["ranked_table"][0]["label"], "AR=1")
        self.assertNotIn("svg", result["ranked_table"][0])
        self.assertEqual(result["source"], "CACHED_ORFS")
        self.assertIn("AR=1", result["verdict"])

    def test_query_memory_recalls_floorplans_even_when_dse_memory_is_cold(self):
        import os
        from adaptive_iicpilot.chat import Session
        from adaptive_iicpilot.sqlite_memory import SQLiteMemory

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "m.db")
            with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY_DB": db}):
                session = Session(emit=lambda s: None)
            self.assertIsInstance(session.memory, SQLiteMemory)
            fp = FloorplanMemory(db)
            k = FloorplanKnobs(design="tinyRocket", macro_mode="manual", macro_strategy="periphery")
            fp.set_best("tinyRocket", "balanced", fake_result(k, "FULL", 0.9, svg="/s.svg"), 1.0)
            fp.record_correction("tinyRocket", "macro_move", "m0", [1, 2], [3, 4])
            fp.close()
            result = session.dispatch("query_memory", {"design": "tinyRocket"})
            session.memory.close()
        self.assertFalse(result["warm"])
        self.assertEqual(result["floorplan"]["best_by_objective"]["balanced"]["floorplan"], "manual=periphery")
        self.assertEqual(result["floorplan"]["engineer_macro_moves"][0]["to"], [3, 4])

    def test_jsonl_writes_image_frames_and_images_on_proposals(self):
        import io
        import os
        from contextlib import redirect_stdout
        from adaptive_iicpilot.chat_jsonl import JsonlBackend, _image
        from adaptive_iicpilot.types import FlowConfig, Proposal

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _image({"title": "t", "svg": "<svg/>", "path": "/p.svg"})
        self.assertEqual(json.loads(buffer.getvalue()), {"type": "image", "title": "t", "svg": "<svg/>", "path": "/p.svg"})

        with mock.patch.dict(os.environ, {"ADAPTIVE_IICPILOT_MEMORY": "inmemory"}):
            backend = JsonlBackend()
        backend.hitl = True
        for image, expect in (("<svg/>", True), (None, False)):
            backend.decisions.put({"type": "decision", "decision": "accept"})
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                backend._decide(Proposal({"aspect_ratio": 1.0}, "r", "FloorplanAgent", image_svg=image),
                                FlowConfig(design="ibex"))
            frame = json.loads(buffer.getvalue().splitlines()[0])
            self.assertEqual("image_svg" in frame, expect)


if __name__ == "__main__":
    unittest.main()
