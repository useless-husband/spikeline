#!/usr/bin/env python3
"""Throughput, minimum clock frequency and latency, measured on the
Verilator model of the chip (build/vsim_gals: every PE in its own clock
domain).

1. Cycles per sample.  One PE's clock is made slow (10 MHz) and everything
   else fast, so that PE is the bottleneck; then
       cycles/sample = f_PE x (time of the last output - time of the first input) / samples.
   Run on a synthetic recording (SNR 5), so data-dependent PEs (snippet,
   Rice) show their cost on realistic data.
2. Real-time check.  Samples arrive like from a real ADC: one every
   347.222 ns (96 x 30 kHz), and the harness counts every sample the chip
   could not take before the next one arrived (an overrun).  Each PE is run
   just above and just below its minimum frequency, the others fast.
3. Latency.  Real-time input, all PEs at modest clocks, spike detection and
   compression in parallel; NEO output probed on out2.  Detection latency =
   time the NEO word with det = 1 leaves the chip - ADC time of that sample
   (includes the one-frame wait for the next sample that the energy operator
   needs).  Snippet latency = time of the packet header - ADC time of the
   aligned peak sample (includes the POST samples the window must wait for).

    python3 tools/bench.py   -> docs/results/throughput.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "model"), str(REPO / "tests" / "system")]

import chip as C  # noqa: E402
import slmodel as M  # noqa: E402
import synthdata as S  # noqa: E402

NCH = 96
RATE = NCH * 30000                 # samples per second
ADC_PS = 347222                    # 1 / 2.88 MHz, rounded down (slightly faster than real time)
FAST = 2000                        # 500 MHz for the clocks that should not matter
NOISE_QUICK = M.cfg_noise(step=64, warmup=64)

# PE -> (route list ending at out0, worst-case cycles per sample from the RTL)
PIPES = {
    "bp": ([("bp", "in"), ("out0", "bp")], 13.0),
    "noise": ([("noise", "in"), ("out0", "noise")], 1.0),
    "neo": ([("noise", "in"), ("neo", "noise"), ("out0", "neo")], 1.0),
    "snip": ([("bp", "in"), ("noise", "bp"), ("neo", "noise"), ("snip", "neo"), ("out0", "snip")],
             1.0 + (M.SNIP_LEN + 1) / (M.POST + 1)),
    "pred": ([("pred", "in"), ("out0", "pred")], 1.0),
    "rice": ([("pred", "in"), ("rice", "pred"), ("out0", "rice")], 2.0 + 1.0 / (NCH << 7)),
}
WORST_NOTE = {
    "bp": "1 accept + 11 multiply-accumulate + 1 output cycle, every sample",
    "noise": "one sample per cycle",
    "neo": "one sample per cycle",
    "snip": "1 per sample + 33 per packet; a channel can start a new packet at most every POST + 1 = 22 frames",
    "pred": "one sample per cycle",
    "rice": "2 cycles if every sample needs an escape code, + 1 flush cycle per block",
}


def recording(frames, snr=5, seed=2001):
    x = S.make_recording(frames / 30000 + 0.001, snr=snr, seed=seed)["x"].astype(np.int64)
    return x[:frames]


def periods(slow=None, slow_ps=None, base=FAST, fab=FAST):
    p = {k: base for k in C.CLOCKS}
    p["fab"] = fab
    if slow:
        p[slow] = slow_ps
    return p


def cycles_per_sample(x):
    rows = []
    for pe, (routes, worst) in PIPES.items():
        s = C.Script().drain().pe_cfg("noise", NOISE_QUICK, clear=True).routes(routes)
        s.feed(len(x)).drain()
        slow_ps = 100000                                       # 10 MHz
        res = C.run(f"bench_cps_{pe}", s, x, periods=periods(pe, slow_ps), seed=1)
        t0 = res.feeds[0][1]
        t1 = int(res.times(0).max())
        cps = (t1 - t0) / slow_ps / x.size
        rows.append((pe, cps, worst))
    return rows


def realtime(x, pe, mhz):
    s = C.Script().drain().pe_cfg("noise", NOISE_QUICK, clear=True).routes(PIPES[pe][0])
    s.feed(len(x)).drain()
    p = periods(pe, int(round(1e6 / mhz)), base=4000, fab=25000)          # others 250 MHz, fabric 40 MHz
    res = C.run(f"bench_rt_{pe}_{mhz:.3f}", s, x, periods=p, adc_period=ADC_PS, seed=2)
    return res.stats["overruns"]


def latency(x):
    clocks = {"fab": 10.0, "bp": 40.0, "noise": 3.2, "neo": 3.2, "snip": 4.0, "pred": 3.2, "rice": 3.6}
    s = C.Script().drain().pe_cfg("noise", NOISE_QUICK, clear=True)
    s.routes(C.BOTH + [("out2", "neo")]).feed(len(x)).drain()
    res = C.run("bench_latency", s, x, periods={k: int(round(1e6 / v)) for k, v in clocks.items()},
                adc_period=ADC_PS, seed=3)
    w0, t0 = res.feeds[0]
    assert w0 == 0
    adc_time = lambda frame, ch: t0 + (frame * NCH + ch) * ADC_PS          # noqa: E731
    neo = res.port(2)
    tn = res.times(2)
    idx = np.nonzero((neo[:, 0] >> 16) & 1)[0]
    det_lat = [(tn[i] - adc_time(i // NCH - 1, i % NCH)) / 1e6 for i in idx]      # NEO word i = sample i - 1 frame
    snip = res.port(0)
    ts = res.times(0)
    hdr = [0]
    while hdr[-1] + M.SNIP_LEN + 1 < len(snip):
        hdr.append(hdr[-1] + M.SNIP_LEN + 1)
    sn_lat = [(ts[h] - adc_time(int(snip[h, 0]) - 1, int(snip[h, 1]))) / 1e6 for h in hdr]
    return clocks, np.array(det_lat), np.array(sn_lat), res.stats


def main():
    out = ["# Throughput, minimum clock frequency and latency", "",
           "Generated by `make bench` (tools/bench.py) on the Verilator model with every PE in its own "
           "clock domain.  Input: synthetic recording, SNR 5, seed 2001.  Real time = 96 channels x 30 kHz "
           f"= {RATE / 1e6:.2f} M samples/s.", ""]
    x = recording(1200)
    rows = cycles_per_sample(x)
    out += ["## Cycles per sample and minimum clock", "",
            "| PE | measured cycles/sample (this data) | worst case cycles/sample | why | minimum clock for 96 x 30 kHz (worst case) |",
            "|---|---:|---:|---|---:|"]
    for pe, cps, worst in rows:
        out.append(f"| {pe} | {cps:.3f} | {worst:.3f} | {WORST_NOTE[pe]} | {worst * RATE / 1e6:.2f} MHz |")
    out += ["", "## Real-time check (ADC-paced input, 1200 frames = 40 ms)", "",
            "Overruns = samples the chip could not accept before the next one arrived.", "",
            "| PE | clock | overruns |", "|---|---:|---:|"]
    xr = recording(1200)
    for pe, cps, worst in rows:
        need = (worst if pe in ("bp", "noise", "neo", "pred") else cps) * RATE / 1e6
        for factor in (0.97, 1.03):
            mhz = need * factor
            n = realtime(xr, pe, mhz)
            out.append(f"| {pe} | {mhz:.3f} MHz ({factor:.2f} x {need:.3f}) | {n} |")
    clocks, det, sn, stats = latency(recording(1500))
    out += ["", "## Latency (real-time input)", "",
            "Clocks: " + ", ".join(f"{k} {v:g} MHz" for k, v in clocks.items()) +
            f".  Overruns during the run: {stats['overruns']}.", "",
            "| event | count | min (us) | median (us) | max (us) | of which waiting for later samples |",
            "|---|---:|---:|---:|---:|---|",
            f"| NEO detection leaves the chip | {len(det)} | {det.min():.1f} | {np.median(det):.1f} | "
            f"{det.max():.1f} | 33.3 us (one frame: psi[n] needs x[n+1]) |",
            f"| snippet header leaves the chip | {len(sn)} | {sn.min():.1f} | {np.median(sn):.1f} | "
            f"{sn.max():.1f} | {(M.POST + 1) * 1e6 / 30000:.1f} us (POST + 1 frames after the peak) |", ""]
    path = REPO / "docs" / "results" / "throughput.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out))
    print("\n".join(out))


if __name__ == "__main__":
    main()
