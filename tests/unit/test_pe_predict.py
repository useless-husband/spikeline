"""Predictor PE against the model for every order and several block sizes."""

import random

import cocotb
import numpy as np

import slmodel as M
from pecommon import NCH, SEED, start
from simrun import run
from streams import Stream, compare


@cocotb.test()
async def matches_model(dut):
    rng = random.Random(SEED)
    data = np.random.default_rng(SEED)
    for order, blog in [(0, 2), (1, 0), (2, 3), (3, 1), (2, 15), (1, 5)]:
        cfg = M.cfg_predict(order, blog)
        await start(dut, cfg)
        x = np.cumsum(data.normal(0, 3000, (150, NCH)), axis=0)
        x[70:75] = data.choice([-32768, 32767], (5, NCH))     # largest residuals
        x = np.clip(np.round(x), -32768, 32767).astype(np.int64)
        m = M.Predictor(NCH)
        m.configure(cfg)
        want = M.Predictor.words(*m.run(x))
        got = await Stream(dut, dut.clk, rng).run(M.input_words(x), len(want), p_valid=0.8, p_ready=0.8)
        compare(got, want, f"order {order} block 2^{blog}")


def test_pe_predict():
    run("pe_predict", ["pe_predict.sv"], "test_pe_predict", parameters={"NCH": NCH})
