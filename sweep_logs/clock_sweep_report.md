# Clock-period sweeps - real ORFS, nangate45

Every row is a measured run whose period was proven from its own synthesis log (ABC `-D`) and 6_final.sdc; floorplan knobs were proven from 2_1_floorplan.log. No estimates.

### ibex (util 80.0, AR 1, density auto; stock clock 2.2 ns; timing limit -0.11 ns from rules-base.json)

fmax_mhz and cp_delay_ns are **reg-to-reg only** (core_clock period_min). WNS / timing_status cover every path group; worst_path_group is the class of the path that sets WNS. `ORFS fmax` is the raw finish__timing__fmax (max over clocks) and the clock it came from.

| clock_ns | x stock | fmax_mhz (reg2reg only) | cp_delay_ns (reg2reg only) | ORFS fmax (clock) | wns_ns | worst_path_group | worst reg2reg slack | timing_status (limit) | congestion | power_mw | power vs stock | die_um2 | runtime | flow rc | SDC proof |
|---:|---:|---:|---:|---|---:|---|---:|---|---:|---:|---:|---:|---:|---|---|
| 1.76 | 0.8 | 456.8 | 2.1889 | 591.0 (vclk_core_clock) | -0.4289 | reg2reg (core_clock) | -0.4289 | VIOLATED (-0.11) | 1.0026 **congested** | 29.045 | +34.2% | 35,944.4 | 29.0 min | 0 | OK (ABC -D 1.76) |
| 1.98 | 0.9 | 468.1 | 2.1361 | 566.7 (vclk_core_clock) | -0.1561 | reg2reg (core_clock) | -0.1561 | VIOLATED (-0.11) | 0.9782 | 24.782 | +14.5% | 35,944.4 | 30.8 min | 0 | OK (ABC -D 1.98) |
| 2.2 | 1 | 453.1 | 2.2072 | 532.0 (vclk_core_clock) | -0.0072 | reg2reg (core_clock) | -0.0072 | ACCEPTABLE (-0.11) | 0.9737 | 21.643 | +0.0% | 35,944.4 | 11.7 min | 0 | OK (ABC -D 2.2) |
| 2.42 | 1.1 | 414.6 | 2.4117 | 513.0 (vclk_core_clock) | +0.0083 | reg2reg (core_clock) | +0.0083 | MET (-0.11) | 1.0037 **congested** | 19.534 | -9.7% | 35,944.4 | 10.0 min | 0 | OK (ABC -D 2.42) |
| 2.75 | 1.25 | 377.7 | 2.6474 | 477.7 (vclk_core_clock) | +0.1026 | reg2reg (core_clock) | +0.1026 | MET (-0.11) | 0.9969 | 17.253 | -20.3% | 35,944.4 | 10.6 min | 0 | OK (ABC -D 2.75) |
| 2.8 | 1.2727 | 382.4 | 2.6149 | 473.6 (vclk_core_clock) | +0.1851 | reg2reg (core_clock) | +0.1851 | MET (-0.11) | 0.9712 | 16.966 | -21.6% | 35,944.4 | 10.0 min | 0 | OK (ABC -D 2.8) |

**Where ibex's timing status comes from:** WNS (finish__timing__setup__ws, all path groups) vs the design's ORFS limit; the WNS path is reg2reg at every clock. fmax/cp_delay columns are reg2reg only. ORFS's headline fmax comes from the virtual I/O clock vclk_core_clock (in2out paths), so it is NOT used for cp_delay.

**Fastest clock that is not VIOLATED and not congested:** 2.2 ns (ACCEPTABLE, wns -0.0072 ns, fmax 453.1 MHz, power 21.643 mW)

#### Determinism check

Clock 2.2 ns, two fresh proven runs (variants `clk_7a0d7c1b0d2caf9f` and `clk_7a0d7c1b0d2caf9f_r2`):

| metric | run 1 | run 2 | difference |
|---|---:|---:|---:|
| wns | -0.00720068 | -0.00720068 | +0 |
| tns | -0.253833 | -0.253833 | +0 |
| die | 35944.4 | 35944.4 | +0 |
| power_w | 0.0216435 | 0.0216435 | +0 |
| fmax_hz | 532012000.0 | 532012000.0 | +0 |
| congestion | 0.973745 | 0.973745 | +0 |
| drc | 0 | 0 | +0 |
| cp_delay_ns (reg2reg, core_clock) | 2.207199 | 2.207199 | +0 |

Runtime: 11.7 min vs 11.5 min.

**Verdict: bit-identical on every metric.**

#### Proven runs vs the earlier unproven cache entries

| clock_ns | wns (legacy -> proven) | power_mw | die_um2 | congestion | verdict |
|---:|---|---|---|---|---|
| 1.76 | -0.42891 -> -0.42891 | 29.045 -> 29.045 | 35944 -> 35944 | 1.0026 -> 1.0026 | identical |
| 1.98 | -0.15608 -> -0.15608 | 24.782 -> 24.782 | 35944 -> 35944 | 0.97816 -> 0.97816 | identical |
| 2.2 | -0.0072007 -> -0.0072007 | 21.643 -> 21.643 | 35944 -> 35944 | 0.97374 -> 0.97374 | identical |
| 2.42 | 0.0082518 -> 0.0082518 | 19.534 -> 19.534 | 35944 -> 35944 | 1.0037 -> 1.0037 | identical |
| 2.75 | 0.10262 -> 0.10262 | 17.253 -> 17.253 | 35944 -> 35944 | 0.9969 -> 0.9969 | identical |

### aes (util stock FLOORPLAN_DEF, AR 1, density auto; stock clock 0.82 ns; timing limit -0.0667 ns from rules-base.json)

fmax_mhz and cp_delay_ns are **reg-to-reg only** (clk period_min). WNS / timing_status cover every path group; worst_path_group is the class of the path that sets WNS. `ORFS fmax` is the raw finish__timing__fmax (max over clocks) and the clock it came from.

| clock_ns | x stock | fmax_mhz (reg2reg only) | cp_delay_ns (reg2reg only) | ORFS fmax (clock) | wns_ns | worst_path_group | worst reg2reg slack | timing_status (limit) | congestion | power_mw | power vs stock | die_um2 | runtime | flow rc | SDC proof |
|---:|---:|---:|---:|---|---:|---|---:|---|---:|---:|---:|---:|---:|---|---|
| 0.656 | 0.8 | 1270.7 | 0.7870 | 1270.7 (clk) | -0.1310 | reg2reg (clk) | -0.1310 | VIOLATED (-0.0667) | 0.8958 | 649.971 | +47.5% | 62,612.4 | 8.8 min | 0 | OK (ABC -D 0.656) |
| 0.738 | 0.9 | 1251.7 | 0.7989 | 1251.7 (clk) | -0.0609 | reg2reg (clk) | -0.0609 | ACCEPTABLE (-0.0667) | 0.8843 | 601.621 | +36.5% | 62,612.4 | 9.0 min | 0 | OK (ABC -D 0.738) |
| 0.82 | 1 | 1199.6 | 0.8336 | 1199.6 (clk) | -0.0136 | reg2reg (clk) | -0.0136 | ACCEPTABLE (-0.0667) | 0.8681 | 440.636 | +0.0% | 62,612.4 | 8.4 min | 0 | OK (ABC -D 0.82) |
| 0.902 | 1.1 | 1113.6 | 0.8980 | 1113.6 (clk) | +0.0040 | reg2reg (clk) | +0.0040 | MET (-0.0667) | 0.8521 | 373.719 | -15.2% | 62,612.4 | 8.0 min | 0 | OK (ABC -D 0.902) |
| 1.025 | 1.25 | 1086.9 | 0.9201 | 1086.9 (clk) | +0.1010 | in2reg (clk) | +0.1049 | MET (-0.0667) | 0.8592 | 319.352 | -27.5% | 62,612.4 | 8.5 min | 0 | OK (ABC -D 1.025) |

**Where aes's timing status comes from:** WNS (finish__timing__setup__ws, all path groups) vs the design's ORFS limit; the WNS path is in2reg / reg2reg (non-reg2reg at 1.025 ns). fmax/cp_delay columns are reg2reg only. ORFS's headline fmax is the same real clock.

**Fastest clock that is not VIOLATED and not congested:** 0.738 ns (ACCEPTABLE, wns -0.0609 ns, fmax 1251.7 MHz, power 601.621 mW)

### gcd (util 55.0, AR 1, density auto; stock clock 0.25 ns; timing limit -0.193 ns from rules-base.json)

fmax_mhz and cp_delay_ns are **reg-to-reg only** (core_clock period_min). WNS / timing_status cover every path group; worst_path_group is the class of the path that sets WNS. `ORFS fmax` is the raw finish__timing__fmax (max over clocks) and the clock it came from.

| clock_ns | x stock | fmax_mhz (reg2reg only) | cp_delay_ns (reg2reg only) | ORFS fmax (clock) | wns_ns | worst_path_group | worst reg2reg slack | timing_status (limit) | congestion | power_mw | power vs stock | die_um2 | runtime | flow rc | SDC proof |
|---:|---:|---:|---:|---|---:|---|---:|---|---:|---:|---:|---:|---:|---|---|
| 0.2 | 0.8 | 2415.8 | 0.4139 | 2415.8 (core_clock) | -0.2139 | reg2reg (core_clock) | -0.2139 | VIOLATED (-0.193) | 0.7193 | 8.085 | +26.8% | 1,278.4 | 3.7 min | 0 | OK (ABC -D 0.2) |
| 0.225 | 0.9 | 2387.4 | 0.4189 | 2387.4 (core_clock) | -0.1939 | reg2reg (core_clock) | -0.1939 | VIOLATED (-0.193) | 0.7168 | 7.150 | +12.1% | 1,278.4 | 5.3 min | 0 | OK (ABC -D 0.225) |
| 0.25 | 1 | 2435.3 | 0.4106 | 2435.3 (core_clock) | -0.1606 | reg2reg (core_clock) | -0.1606 | ACCEPTABLE (-0.193) | 0.7243 | 6.377 | +0.0% | 1,278.4 | 3.0 min | 0 | OK (ABC -D 0.25) |
| 0.275 | 1.1 | 2364.2 | 0.4230 | 2364.2 (core_clock) | -0.1480 | reg2reg (core_clock) | -0.1480 | ACCEPTABLE (-0.193) | 0.7168 | 5.721 | -10.3% | 1,278.4 | 2.0 min | 0 | OK (ABC -D 0.275) |
| 0.3125 | 1.25 | 2390.3 | 0.4184 | 2390.3 (core_clock) | -0.1059 | reg2reg (core_clock) | -0.1059 | ACCEPTABLE (-0.193) | 0.7183 | 4.785 | -25.0% | 1,278.4 | 1.9 min | 0 | OK (ABC -D 0.3125) |

**Where gcd's timing status comes from:** WNS (finish__timing__setup__ws, all path groups) vs the design's ORFS limit; the WNS path is reg2reg at every clock. fmax/cp_delay columns are reg2reg only. ORFS's headline fmax is the same real clock.
Near-tie (within 0.01 ns at the report's 2-decimal resolution, so the class of the WNS path is not robust): 0.2 ns: reg2out -0.20 vs reg2reg -0.21; 0.225 ns: reg2out -0.19 vs reg2reg -0.19; 0.25 ns: reg2out -0.15 vs reg2reg -0.16; 0.275 ns: reg2out -0.14 vs reg2reg -0.15; 0.3125 ns: reg2out -0.10 vs reg2reg -0.11.

**Fastest clock that is not VIOLATED and not congested:** 0.25 ns (ACCEPTABLE, wns -0.1606 ns, fmax 2435.3 MHz, power 6.377 mW)

### ibex @ 2.8 ns vs IICPilot paper (different OpenROAD versions, not a direct benchmark)

| | cp_delay_ns | area_um2 |
|---|---:|---:|
| IICPilot paper (ibex row) | 2.2569 | 48,305 |
| this run (util 80, 2.8 ns) | 2.6149 | 35,944.4 (die) / 28,410.7 (cell) |

The paper does not say whether its area is die or cell area, so both are shown.

Charts: sweep_logs/figures/power_vs_clock.png, sweep_logs/figures/fmax_vs_clock.png
