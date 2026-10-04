#!/usr/bin/env python3
"""Mutation check: do the tests notice a broken design?

Each mutant is a one-line change to the RTL or to the golden model.  For
every mutant the tool copies the sources into build/mutants/<id>/, applies
the change, rebuilds what the change touches and runs the test suites that
should notice it.  A mutant is "killed" when at least one suite fails.

    python3 tools/mutate.py            # all mutants, Markdown table on stdout
    python3 tools/mutate.py rice_k     # just one
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "build" / "mutants"
COPY = ["Makefile", "rtl", "sim", "model", "tests", "tools"]

U = "tests/unit/"
SYS = "tests/system/test_pipelines.py"

# id, file, original text, replacement, what it breaks, suites
MUTANTS = [
    ("bp_coef", "rtl/pe_bandpass.sv", "op_a = u2;                      op_b = S0_B2;",
     "op_a = u2;                      op_b = S0_B1;", "band-pass: wrong coefficient for x[n-2]",
     [U + "test_pe_bandpass.py::test_pe_bandpass"]),
    ("bp_truncate", "rtl/pe_bandpass.sv", "t = (a + 48'sd32768) >>> 16;", "t = a >>> 16;",
     "band-pass: section output truncated instead of rounded", [U + "test_pe_bandpass.py::test_pe_bandpass"]),
    ("noise_tie", "rtl/pe_noise.sv", "wire [23:0] m_next = a > {1'b0, mc} ? m_up",
     "wire [23:0] m_next = a >= {1'b0, mc} ? m_up", "noise: median tracker steps up on a tie",
     [U + "test_pe_noise.py"]),
    ("noise_ready", "rtl/pe_noise.sv", "wire        ready = fcnt >= warmup;", "wire        ready = fcnt > warmup;",
     "noise: warm-up one frame too long", [U + "test_pe_noise.py"]),
    ("neo_refractory", "rtl/pe_neo.sv", "wire det = rdy && (amp_mode ? hit_a : hit_e) && r == 8'd0;",
     "wire det = rdy && (amp_mode ? hit_a : hit_e);", "NEO: refractory period ignored", [U + "test_pe_neo.py"]),
    ("neo_sign", "rtl/pe_neo.sv", "wire signed [32:0] psi  = xc33 * xc33 - xp33 * xn33;",
     "wire signed [32:0] psi  = xc33 * xc33 + xp33 * xn33;", "NEO: wrong sign in the energy operator",
     [U + "test_pe_neo.py"]),
    ("snip_tie", "rtl/pe_snippet.sv", "wire better    = searching && ax > peak;",
     "wire better    = searching && ax >= peak;", "snippet: aligns to the last of equal peaks",
     [U + "test_pe_snippet.py::test_pe_snippet"]),
    ("snip_window", "rtl/pe_snippet.sv", "raddr_n = {ch, wslot - BACK};", "raddr_n = {ch, wslot - BACK + 1'b1};",
     "snippet: window shifted by one sample", [U + "test_pe_snippet.py::test_pe_snippet"]),
    ("pred_order2", "rtl/pe_predict.sv", "x_e - {x1_e[18:0], 1'b0} + x2_e", "x_e - {x1_e[18:0], 1'b0} - x2_e",
     "predictor: wrong second-order prediction", [U + "test_pe_predict.py"]),
    ("pred_block", "rtl/pe_predict.sv", "wire        lastblk = fb >= mask && in_last;",
     "wire        lastblk = fb > mask && in_last;", "predictor: block end never flagged",
     [U + "test_pe_predict.py"]),
    ("rice_k", "rtl/pe_rice.sv", "if (m >= (20'd1 << i)) k = k + 4'd1;", "if (m > (20'd1 << i)) k = k + 4'd1;",
     "Rice: k off by one when the mean is a power of two", [U + "test_pe_rice.py"]),
    ("rice_last", "rtl/pe_rice.sv", "out_last <= ch_final && rest == 6'd0;", "out_last <= ch_final;",
     "Rice: `last` on a word that is not the block's final word", [U + "test_pe_rice.py"]),
    ("rice_reset", "rtl/pe_rice.sv", "wire [23:0] a_cur   = first ? A_INIT : acc[ch];",
     "wire [23:0] a_cur   = acc[ch];", "Rice: adaptation not restarted per block",
     [U + "test_pe_rice.py"]),
    ("afifo_full", "rtl/sl_async_fifo.sv", "wire [AW:0] full_val = {~rgray_w[AW:AW-1], rgray_w[AW-2:0]};",
     "wire [AW:0] full_val = {~rgray_w[AW], rgray_w[AW-1:0]};", "async FIFO: binary full test on Gray pointers",
     [U + "test_async_fifo.py::test_async_fifo_depth8"]),
    ("afifo_gray", "rtl/sl_async_fifo.sv", "wire [AW:0] wgray_n = (wbin_n >> 1) ^ wbin_n;",
     "wire [AW:0] wgray_n = wbin_n;", "async FIFO: write pointer crosses in binary",
     [U + "test_async_fifo.py::test_async_fifo_depth8"]),
    ("cdc_early_ack", "rtl/sl_cfg_cdc.sv",
     "assign applied = (take && !word[31]) || (pend && pe_idle && started);", "assign applied = take;",
     "config crossing: acknowledged before the clear sweep ran",
     [U + "test_cfg_cdc.py", "tests/system/test_realtime.py::test_clear_then_feed_keeps_up"]),
    ("xbar_fork", "rtl/sl_xbar.sv", "if (rst || sel_wr || mine_fired[d]) done[d] <= 1'b0;",
     "if (rst || sel_wr) done[d] <= 1'b0;", "crossbar: multicast never re-arms", [SYS]),
    ("xbar_route", "rtl/sl_xbar.sv", "assign dst_bus[d*W +: W] = src_pad[s[2:0]*W +: W];",
     "assign dst_bus[d*W +: W] = src_pad[s[1:0]*W +: W];", "crossbar: route select loses its top bit", [SYS]),
    ("model_zigzag", "model/sl_decode.py", "e = (u >> 1) ^ -(u & 1)", "e = u >> 1",
     "decoder: zigzag sign lost", ["tests/model/test_model.py"]),
]


def sh(cmd, cwd, timeout=1800):
    p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout[-3000:] + p.stderr[-3000:]


def run_mutant(m):
    mid, path, orig, repl, what, suites = m
    d = WORK / mid
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for c in COPY:
        src = REPO / c
        (shutil.copytree if src.is_dir() else shutil.copy)(src, d / c)
    if path:
        f = d / path
        text = f.read_text()
        assert text.count(orig) == 1, f"{mid}: original text found {text.count(orig)} times in {path}"
        f.write_text(text.replace(orig, repl))
    py = sys.executable
    results = []
    if any(s.startswith("tests/system") for s in suites):
        # system suites need the simulator built from the mutated RTL
        rc, log = sh("make build/vsim_gals", d)
        if rc:
            return mid, path, what, "ERROR", "the mutated design did not build: " + log[-200:]
    for s in suites:
        rc, _ = sh(f'"{py}" -m pytest -q -x {s}', d)
        results.append(f"{s.split('/')[-1]}: {'FAIL' if rc else 'pass'}")
    killed = any("FAIL" in r for r in results)
    return mid, path, what, "killed" if killed else "SURVIVED", ", ".join(results)


def control():
    """The unmutated copy must pass every suite, or a 'kill' means nothing."""
    suites = sorted({s for m in MUTANTS for s in m[5]})
    r = run_mutant(("control", "", "", "", "no change", suites))
    shutil.rmtree(WORK / "control", ignore_errors=True)
    if "FAIL" in r[4] or r[3] == "ERROR":
        sys.exit(f"control run failed, mutation results would be meaningless: {r[4]}")
    print(f"control (no change): all {len(suites)} suites pass", file=sys.stderr)


def main():
    pick = sys.argv[1:]
    control()
    rows = []
    for m in MUTANTS:
        if pick and m[0] not in pick:
            continue
        r = run_mutant(m)
        print(f"{r[0]}: {r[3]} ({r[4]})", file=sys.stderr)
        rows.append(r)
        shutil.rmtree(WORK / m[0], ignore_errors=True)
    out = ["| Mutant | File | What it breaks | Result | Suites |", "|---|---|---|---|---|"]
    out += [f"| `{a}` | `{b}` | {c} | {d} | {e} |" for a, b, c, d, e in rows]
    print("\n".join(out))
    if any(r[3] != "killed" for r in rows):
        sys.exit(1)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    main()
