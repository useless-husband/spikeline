#!/usr/bin/env python3
"""Render what the simulated chip does to a few channels.

Runs the Verilator model of the chip (build/vsim_gals) on a 0.4 s synthetic
recording with spike detection and compression in parallel, and the noise
PE's output (filtered signal + noise estimate) probed on out2.  Every trace
in the picture except the raw input comes from words the hardware emitted:

    raw input            what the chip received
    band-passed signal   data[15:0] of the noise PE's output words
    NEO energy psi       x[n]^2 - x[n-1]x[n+1] of that band-passed signal,
                         with the chip's threshold (noise^2 * C >> 16, from the
                         same words) -- the comparison the NEO PE makes
    detected spikes      aligned peaks from the snippet packets (out0)
    snippets             the 32-sample windows in those packets
    ground truth         the generator's spike times, for comparison

    python3 tools/demo.py [--out build/demo/demo.png]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "model"), str(REPO / "tests" / "system")]

import chip as C  # noqa: E402
import slmodel as M  # noqa: E402
import synthdata as S  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
FS = 30000
CHANNELS = (5, 17, 40)
WINDOW = (0.20, 0.30)          # seconds shown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "build" / "demo" / "demo.png"))
    a = ap.parse_args()

    rec = S.make_recording(0.4, snr=5, seed=7)
    x = rec["x"].astype(np.int64)
    frames = len(x) // 128 * 128
    x = x[:frames]
    s = C.Script().drain().routes(C.BOTH + [("out2", "noise")]).feed(frames).drain()
    res = C.run("demo", s, x, periods={"bp": 2500}, seed=7)

    probe = res.port(2)[:, 0].reshape(frames, M.NCH)
    filt = ((probe & 0xFFFF) ^ 0x8000) - 0x8000
    noise = (probe >> 16) & 0x7FFF
    thr = (noise * noise * M.NEO_C_DEFAULT) >> 16
    psi = np.zeros_like(filt)
    psi[1:-1] = filt[1:-1] ** 2 - filt[:-2] * filt[2:]

    snip = res.port(0)
    packets = []
    i = 0
    while i + M.SNIP_LEN < len(snip):
        packets.append((int(snip[i, 1]), int(snip[i, 0]) - 1,
                        ((snip[i + 1:i + 1 + M.SNIP_LEN, 0] & 0xFFFF) ^ 0x8000) - 0x8000))
        i += M.SNIP_LEN + 1
    comp_words = len(res.port(1))
    ratio = x.size * 16 / (32 * comp_words)

    lo, hi = int(WINDOW[0] * FS), int(WINDOW[1] * FS)
    t = np.arange(lo, hi) / FS * 1e3

    plt.rcParams.update({"font.size": 9, "axes.edgecolor": INK2, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "axes.titlecolor": INK,
                         "axes.titlelocation": "left", "axes.titlesize": 10})
    fig = plt.figure(figsize=(12, 8.2), facecolor=SURFACE)
    gs = fig.add_gridspec(3, 4, width_ratios=[1, 1, 1, 0.55], hspace=0.55, wspace=0.35,
                          left=0.06, right=0.98, top=0.9, bottom=0.07)
    fig.text(0.06, 0.965, "spikeline: chip output for three of 96 channels (Verilator simulation)",
             fontsize=12, color=INK, ha="left", weight="bold")
    fig.text(0.06, 0.937, f"Synthetic recording, SNR 5, seed 7.  {len(packets)} snippet packets in 0.4 s; "
             f"raw stream compressed {ratio:.2f}x losslessly in parallel.",
             fontsize=9, color=INK2, ha="left")

    for row, ch in enumerate(CHANNELS):
        det = np.array([p for c, p, _ in packets if c == ch and lo <= p < hi], dtype=np.int64)
        truth = rec["gt_time"][(rec["gt_chan"] == ch) & (rec["gt_time"] >= lo) & (rec["gt_time"] < hi)]
        ax = fig.add_subplot(gs[row, 0])
        ax.plot(t, x[lo:hi, ch], color=INK2, lw=0.6)
        ax.set_title(f"channel {ch}: raw input", pad=4)
        ax.set_ylabel("LSB (0.195 uV)")

        ax = fig.add_subplot(gs[row, 1])
        ax.plot(t, filt[lo:hi, ch], color=BLUE, lw=0.7)
        ax.plot(truth / FS * 1e3, np.full(len(truth), filt[lo:hi, ch].max() * 1.15), "v", color=AQUA,
                ms=5, label="true spike")
        ax.plot(det / FS * 1e3, filt[det, ch], "o", mfc="none", mec=ORANGE, mew=1.4, ms=8, label="detected peak")
        ax.set_title("band-passed (300-6000 Hz) and detections", pad=4)
        if row == 0:
            handles = ax.get_legend_handles_labels()

        ax = fig.add_subplot(gs[row, 2])
        ax.plot(t, psi[lo:hi, ch], color=BLUE, lw=0.7)
        ax.plot(t, thr[lo:hi, ch], color=ORANGE, lw=1.4)
        ax.set_title("NEO energy psi and adaptive threshold", pad=4)
        ax.set_ylim(min(psi[lo:hi, ch].min(), 0), max(psi[lo:hi, ch].max(), thr[lo:hi, ch].max()) * 1.1)

        ax = fig.add_subplot(gs[row, 3])
        waves = [w for c, _, w in packets if c == ch]
        ts = (np.arange(M.SNIP_LEN) - M.PRE) / FS * 1e3
        for w in waves:
            ax.plot(ts, w, color=BLUE, lw=0.6, alpha=0.35)
        ax.axvline(0, color=GRID, lw=1)
        ax.set_title(f"{len(waves)} aligned snippets", pad=4)
        for a_ in fig.axes[-4:]:
            a_.set_facecolor(SURFACE)
            a_.grid(True, color=GRID, lw=0.6)
            a_.set_axisbelow(True)
            for side in ("top", "right"):
                a_.spines[side].set_visible(False)
        if row == 2:
            for a_ in fig.axes[-4:-1]:
                a_.set_xlabel("time (ms)")
            fig.axes[-1].set_xlabel("ms from peak")

    thr_line = plt.Line2D([], [], color=ORANGE, lw=1.4, label="NEO threshold")
    fig.legend(handles[0] + [thr_line], handles[1] + ["NEO threshold"], loc="upper right",
               bbox_to_anchor=(0.985, 1.0), ncol=3, frameon=False, fontsize=9)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=100, facecolor=SURFACE)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB): {len(packets)} packets, compression {ratio:.3f}x")


if __name__ == "__main__":
    main()
