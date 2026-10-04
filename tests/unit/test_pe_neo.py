"""NEO detector PE against the model in both detection modes, with random
noise levels, thresholds and refractory periods."""

import random

import cocotb
import numpy as np

import slmodel as M
from pecommon import NCH, SEED, start
from simrun import run
from streams import Stream, compare


def stimulus(seed, frames):
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 60, (frames, NCH))
    spikes = rng.random((frames, NCH)) < 0.03
    x[spikes] -= rng.uniform(200, 32000, spikes.sum())
    x[:5] = rng.choice([-32768, 32767], (5, NCH))        # extreme products
    x = np.clip(np.round(x), -32768, 32767).astype(np.int64)
    noise = rng.integers(0, 1 << 15, (frames, NCH))
    noise[: frames // 2] = rng.integers(0, 1500, (frames // 2, NCH))
    ready = np.arange(frames) >= 10
    return x, noise, ready


@cocotb.test()
async def matches_model(dut):
    rng = random.Random(SEED)
    for trial in range(4):
        mode = trial % 2
        c = rng.choice([0, 1, 700, 2400, 65535]) if trial < 2 else rng.randrange(1 << 16)
        refr = rng.choice([0, 1, 30, 255])
        cfg = M.cfg_neo(c, refr, mode)
        await start(dut, cfg)
        x, noise, ready = stimulus(SEED * 10 + trial, 300)
        xd, det = M.Neo(NCH, c, refr, mode).run(x, noise, ready)
        want = M.Neo.words(xd, det)
        words = M.Noise.words(x, noise, ready)
        got = await Stream(dut, dut.clk, rng).run(words, len(want), p_valid=0.8, p_ready=0.8)
        compare(got, want, f"trial {trial} mode {mode} C {c} refr {refr}")
        dut._log.info("trial %d: %d detections", trial, int(det.sum()))


def test_pe_neo():
    run("pe_neo", ["pe_neo.sv"], "test_pe_neo", parameters={"NCH": NCH})
