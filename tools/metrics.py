#!/usr/bin/env python3
"""Detection and compression metrics on synthetic recordings.

    python3 tools/metrics.py            # bit-exact model only (about 2 minutes)
    python3 tools/metrics.py --hw       # also run the Verilator chip on one seed per
                                        # SNR and check it gives the same words

Method
  * Threshold constants are chosen on a calibration recording (seed 1000,
    SNR 5, 1 s) by the best F1, then frozen; detection is scored on other
    seeds (2001-2003, 2 s each, 96 channels) at each SNR.
  * A detection is the aligned peak of a snippet packet.  It matches a true
    spike on the same channel within +-15 samples (0.5 ms), one-to-one.
    The first 2048 frames (noise estimator warm-up) and the last 64 are not
    scored.
  * Compression ratio = 16 bits x samples / bits of the compressed stream,
    including block padding.  Every compressed stream is decoded again and
    compared with the input.
Writes docs/results/detection.md and docs/results/compression.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "model"), str(REPO / "tests" / "system")]

import sl_decode  # noqa: E402
import slmodel as M  # noqa: E402
import synthdata as S  # noqa: E402

CAL_SEED = 1000
EVAL_SEEDS = (2001, 2002, 2003)
SNRS = (2, 3, 4, 5, 6, 8)
TOL = 15
T_LO = 2048
ENERGY_SWEEP = (1800, 2100, 2400, 2550, 2700, 2850, 3000, 3300, 3600)
AMP_SWEEP = (1200, 1300, 1400, 1450, 1500, 1550, 1600, 1700, 1800)


def score(rec, packets):
    t = np.array([p[1] - 1 for p in packets], dtype=np.int64)   # NEO emits one frame late
    c = np.array([p[0] for p in packets], dtype=np.int64)
    n = len(rec["x"])
    return np.array(S.match(t, c, rec["gt_time"], rec["gt_chan"], TOL, T_LO, n - 64))


def prf(tp, fp, fn):
    p = tp / max(tp + fp, 1)
    r = tp / max(tp + fn, 1)
    return p, r, 2 * p * r / max(p + r, 1e-12)


def front_end(x):
    y = M.Bandpass().run(x)
    noise, ready = M.Noise().run(y)
    return y, noise, ready


def detect(y, noise, ready, mode, c):
    xd, det = M.Neo(c=c, mode=mode).run(y, noise, ready)
    return M.Snippet().run(xd, det)


def calibrate(out):
    rec = S.make_recording(1.0, snr=5, seed=CAL_SEED)
    y, noise, ready = front_end(rec["x"].astype(np.int64))
    out += ["## Calibration (seed 1000, SNR 5, 1 s)", "",
            "| Mode | C | Threshold | Precision | Recall | F1 |", "|---|---:|---|---:|---:|---:|"]
    best = {}
    for mode, sweep, form in ((M.MODE_NEO, ENERGY_SWEEP, "psi > {:.1f} x median(|x|)^2"),
                              (M.MODE_AMP, AMP_SWEEP, "|x| > {:.2f} x median(|x|)")):
        for c in sweep:
            p, r, f = prf(*score(rec, detect(y, noise, ready, mode, c)))
            name = "energy (NEO)" if mode == M.MODE_NEO else "amplitude"
            out.append(f"| {name} | {c} | {form.format(c / 256)} | {p:.3f} | {r:.3f} | {f:.3f} |")
            if f > best.get(mode, (0, 0))[1]:
                best[mode] = (c, f)
    out += ["", f"Best F1: energy mode C = {best[0][0]}, amplitude mode C = {best[1][0]}.  "
            f"Defaults in the RTL and model: energy C = {M.NEO_C_DEFAULT}, amplitude C = {M.AMP_C_DEFAULT}.", ""]
    return best


def evaluate(seconds, out):
    rows = {}
    rates = {}
    for snr in SNRS:
        for seed in EVAL_SEEDS:
            rec = S.make_recording(seconds, snr=snr, seed=seed)
            y, noise, ready = front_end(rec["x"].astype(np.int64))
            for mode, c in ((M.MODE_NEO, M.NEO_C_DEFAULT), (M.MODE_AMP, M.AMP_C_DEFAULT)):
                pk = detect(y, noise, ready, mode, c)
                rows[(snr, mode)] = rows.get((snr, mode), 0) + score(rec, pk)
                rates[(snr, mode)] = rates.get((snr, mode), 0) + len(pk) / seconds / len(EVAL_SEEDS)
            rows.setdefault(("truth", snr), 0)
            rows[("truth", snr)] += len(rec["gt_time"])
    out += [f"## Detection versus SNR (seeds {', '.join(map(str, EVAL_SEEDS))}, {seconds:g} s x 96 channels each)", "",
            "SNR = peak of the band-passed spike / standard deviation of the band-passed background.", "",
            "| SNR | true spikes | energy (NEO): precision | recall | F1 | amplitude: precision | recall | F1 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for snr in SNRS:
        e = prf(*rows[(snr, M.MODE_NEO)])
        a = prf(*rows[(snr, M.MODE_AMP)])
        out.append(f"| {snr} | {rows[('truth', snr)]} | {e[0]:.3f} | {e[1]:.3f} | {e[2]:.3f} | "
                   f"{a[0]:.3f} | {a[1]:.3f} | {a[2]:.3f} |")
    out.append("")
    return rows


def residual_entropy(e):
    """Mean zeroth-order entropy (bits) of the residual, per channel."""
    h = []
    for c in range(e.shape[1]):
        _, counts = np.unique(e[:, c], return_counts=True)
        p = counts / counts.sum()
        h.append(-(p * np.log2(p)).sum())
    return float(np.mean(h))


def compression(seconds, out):
    out += [f"## Lossless compression (seed 2001, {seconds:g} s x 96 channels, blocks of 2^7 frames)", "",
            "| SNR | order 0 | order 1 | order 2 | order-1 bits/sample | order-1 residual entropy (bits) | decoded = input |",
            "|---:|---:|---:|---:|---:|---:|---|"]
    for snr in SNRS:
        x = S.make_recording(seconds, snr=snr, seed=EVAL_SEEDS[0])["x"].astype(np.int64)
        frames = len(x) // 128 * 128
        x = x[:frames]
        crs = []
        for order in (0, 1, 2):
            e, first, lastblk = M.Predictor(order=order, block_log2=7).run(x)
            w = M.Rice().run(e, first, lastblk)
            crs.append(x.size * 16 / (32 * len(w)))
            if order == 1:
                bits = 32 * len(w) / x.size
                ent = residual_entropy(e)
                ok = np.array_equal(sl_decode.decode(w[:, 0], 96, frames, 7, 1), x)
        out.append(f"| {snr} | {crs[0]:.3f} | {crs[1]:.3f} | {crs[2]:.3f} | {bits:.2f} | {ent:.2f} | "
                   f"{'yes' if ok else 'NO'} |")
    x = S.make_recording(seconds, snr=5, seed=EVAL_SEEDS[0])["x"].astype(np.int64)
    out += ["", "Block size (order 1, SNR 5).  Smaller blocks lose less data to a dropped radio packet "
            "but restart the prediction and the Rice adaptation more often:", "",
            "| block (frames) | samples per block | compression ratio |", "|---:|---:|---:|"]
    for blog in (3, 5, 7, 9, 11):
        frames = len(x) // (1 << blog) * (1 << blog)
        e, first, lastblk = M.Predictor(order=1, block_log2=blog).run(x[:frames])
        w = M.Rice().run(e, first, lastblk)
        out.append(f"| {1 << blog} | {96 << blog} | {frames * 96 * 16 / (32 * len(w)):.3f} |")
    out.append("")


def hardware(seconds, out):
    import chip as C

    out += [f"## Hardware check (Verilator model of the chip, seed 2001, {seconds:g} s per SNR)", "",
            "Spike detection and compression in parallel (pipeline c), default settings, random "
            "output backpressure.  Every output word is compared with the bit-exact model.", "",
            "| SNR | mode | snippet packets | words compared | identical | compressed words | decoded = input |",
            "|---:|---|---:|---:|---|---:|---|"]
    for snr in SNRS:
        x = S.make_recording(seconds, snr=snr, seed=EVAL_SEEDS[0])["x"].astype(np.int64)
        frames = len(x) // 128 * 128
        x = x[:frames]
        for mode in (M.MODE_NEO, M.MODE_AMP):
            cfg = M.cfg_neo(M.NEO_C_DEFAULT if mode == M.MODE_NEO else M.AMP_C_DEFAULT, mode=mode)
            s = C.Script().drain().pe_cfg("neo", cfg).routes(C.BOTH).feed(frames).drain()
            res = C.run(f"metrics_{snr}_{mode}", s, x, periods={"bp": 2500}, ready=(0.8, 0.8, 1), seed=snr)
            chip = M.Chip(neo_cfg=cfg)
            pk = chip.spike(x)["packets"]
            want_s = M.Snippet.words(pk)
            want_c = chip.compress(x)["words"]
            got_s, got_c = res.port(0), res.port(1)
            same = (got_s.shape == want_s.shape and np.array_equal(got_s, want_s)
                    and got_c.shape == want_c.shape and np.array_equal(got_c, want_c))
            ok = np.array_equal(sl_decode.decode(got_c[:, 0], 96, frames, 7, 1), x)
            out.append(f"| {snr} | {'energy' if mode == M.MODE_NEO else 'amplitude'} | {len(pk)} | "
                       f"{len(got_s) + len(got_c)} | {'yes' if same else 'NO'} | {len(got_c)} | "
                       f"{'yes' if ok else 'NO'} |")
            assert same and ok, f"hardware differs from the model at SNR {snr}"
    out.append("")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--hw", action="store_true")
    ap.add_argument("--out", default=str(REPO / "docs" / "results"))
    a = ap.parse_args()
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    det = ["# Spike detection results", "", "Generated by `make metrics` (tools/metrics.py) from the "
           "bit-exact model; `--hw` adds the hardware check.", ""]
    calibrate(det)
    evaluate(a.seconds, det)
    if a.hw:
        hardware(a.seconds, det)
    (outdir / "detection.md").write_text("\n".join(det))
    comp = ["# Compression results", "", "Generated by `make metrics` (tools/metrics.py).", ""]
    compression(a.seconds, comp)
    (outdir / "compression.md").write_text("\n".join(comp))
    print("\n".join(det + comp))
    print(f"[metrics took {time.time() - t0:.0f} s]", file=sys.stderr)


if __name__ == "__main__":
    main()
