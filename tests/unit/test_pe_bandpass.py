"""Band-pass PE against the bit-exact model: random and full-scale input,
random valid/ready gaps, the 13-cycle schedule, and clear."""

import os
import random

import cocotb
import numpy as np
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

import slmodel as M
from simrun import run
from streams import Stream, compare, reset

NCH = int(os.environ.get("TB_NCH", "8"))


def stimulus(seed, frames):
    rng = np.random.default_rng(seed)
    t = np.arange(frames)[:, None]
    x = (rng.normal(0, 300, (frames, NCH)) + 3000 * np.sin(2 * np.pi * t * rng.uniform(0.001, 0.3, NCH))
         + rng.uniform(-8000, 8000, NCH))
    x[frames // 2: frames // 2 + 20] = 32767          # full-scale step: exercises saturation
    x[frames // 2 + 20: frames // 2 + 40] = -32768
    return np.clip(np.round(x), -32768, 32767).astype(np.int64)


async def start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.cfg.value = 0
    dut.clr.value = 0
    await reset(dut, dut.clk, dut.rst)


@cocotb.test()
async def matches_model_with_backpressure(dut):
    seed = int(os.environ.get("TB_SEED", "1"))
    await start(dut)
    x = stimulus(seed, 200)
    want = M.Bandpass.words(M.Bandpass(NCH).run(x))
    s = Stream(dut, dut.clk, random.Random(seed))
    got = await s.run(M.input_words(x), len(want), p_valid=0.7, p_ready=0.6)
    compare(got, want, f"seed {seed}")


@cocotb.test()
async def thirteen_cycles_per_sample(dut):
    await start(dut)
    x = stimulus(7, 40)
    s = Stream(dut, dut.clk, random.Random(7))
    got = await s.run(M.input_words(x), x.size)
    compare(got, M.Bandpass.words(M.Bandpass(NCH).run(x)))
    gaps = np.diff(s.accept_cycle)
    assert gaps.min() == gaps.max() == 13, (gaps.min(), gaps.max())
    dut._log.info("cycles per sample: %d (worst case = best case)", gaps.max())


@cocotb.test()
async def clear_restarts_from_zero_state(dut):
    await start(dut)
    x = stimulus(3, 60)
    s = Stream(dut, dut.clk, random.Random(3))
    await s.run(M.input_words(x[:30]), 30 * NCH)
    dut.clr.value = 1
    await RisingEdge(dut.clk)
    dut.clr.value = 0
    s2 = Stream(dut, dut.clk, random.Random(4))
    got = await s2.run(M.input_words(x[30:]), 30 * NCH, p_valid=0.5)
    compare(got, M.Bandpass.words(M.Bandpass(NCH).run(x[30:])), "after clear")


def test_pe_bandpass():
    run("pe_bandpass", ["pe_bandpass.sv"], "test_pe_bandpass", parameters={"NCH": NCH})


def test_pe_bandpass_96ch():
    run("pe_bandpass", ["pe_bandpass.sv"], "test_pe_bandpass", parameters={"NCH": 96},
        name="pe_bandpass_96", testcase="matches_model_with_backpressure",
        env={"TB_NCH": "96", "TB_SEED": "2"})
