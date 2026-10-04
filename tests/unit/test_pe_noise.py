"""Noise-estimator PE against the model: per-channel scales from tiny to
full-scale (saturating the 15-bit output), random step sizes and warm-up."""

import random

import cocotb
import numpy as np

import slmodel as M
from pecommon import NCH, SEED, start
from simrun import run
from streams import Stream, compare


def stimulus(seed, frames):
    rng = np.random.default_rng(seed)
    scale = rng.choice([2, 30, 300, 4000, 30000], NCH)
    x = rng.laplace(0, 1, (frames, NCH)) * scale
    return np.clip(np.round(x), -32768, 32767).astype(np.int64)


@cocotb.test()
async def matches_model(dut):
    rng = random.Random(SEED)
    for trial in range(3):
        step, warmup = rng.choice([1, 16, 200, 255]), rng.randrange(0, 60)
        cfg = M.cfg_noise(step, warmup)
        await start(dut, cfg)
        x = stimulus(SEED * 10 + trial, 300)
        model = M.Noise(NCH)
        model.configure(cfg)
        noise, ready = model.run(x)
        want = M.Noise.words(x, noise, ready)
        got = await Stream(dut, dut.clk, rng).run(M.input_words(x), len(want), p_valid=0.8, p_ready=0.7)
        compare(got, want, f"trial {trial} step {step} warmup {warmup}")
        assert ready.any() and not ready.all() or warmup == 0


@cocotb.test()
async def one_sample_per_cycle(dut):
    await start(dut, M.cfg_noise())
    x = stimulus(5, 50)
    s = Stream(dut, dut.clk, random.Random(5))
    await s.run(M.input_words(x), x.size)
    assert set(np.diff(s.accept_cycle)) == {1}


def test_pe_noise():
    run("pe_noise", ["pe_noise.sv"], "test_pe_noise", parameters={"NCH": NCH})
