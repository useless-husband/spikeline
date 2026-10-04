"""Snippet PE against the model: sparse and dense detections, ties, back-
pressure while a packet streams out, and the 96-channel configuration."""

import os
import random

import cocotb
import numpy as np

import slmodel as M
from pecommon import NCH, SEED, start
from simrun import run
from streams import Stream, compare


def stimulus(seed, frames, density):
    rng = np.random.default_rng(seed)
    x = rng.integers(-200, 200, (frames, NCH))
    x[rng.random((frames, NCH)) < 0.02] = -32768          # |x| = 32768 needs 17 bits
    x[rng.random((frames, NCH)) < 0.02] = 150             # ties for the peak search
    det = rng.random((frames, NCH)) < density
    return x.astype(np.int64), det


@cocotb.test()
async def matches_model(dut):
    rng = random.Random(SEED)
    frames = int(os.environ.get("TB_FRAMES", "600"))
    for trial, density in enumerate([0.01, 0.3]):
        await start(dut)
        x, det = stimulus(SEED * 10 + trial, frames, density)
        packets = M.Snippet(NCH).run(x, det)
        want = M.Snippet.words(packets)
        got = await Stream(dut, dut.clk, rng).run(M.Neo.words(x, det), len(want), p_valid=0.9,
                                                  p_ready=0.6)
        compare(got, want, f"density {density}")
        dut._log.info("density %.2f: %d packets", density, len(packets))
        assert len(packets) > 0


def test_pe_snippet():
    run("pe_snippet", ["pe_snippet.sv"], "test_pe_snippet", parameters={"NCH": NCH})


def test_pe_snippet_96ch():
    run("pe_snippet", ["pe_snippet.sv"], "test_pe_snippet", parameters={"NCH": 96},
        name="pe_snippet_96", env={"TB_NCH": "96", "TB_SEED": "3", "TB_FRAMES": "200"})
