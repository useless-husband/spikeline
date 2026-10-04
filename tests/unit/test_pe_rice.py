"""Rice coder PE: bit-exact against the model, and the hardware's own words
decode back to the original samples with the independent software decoder."""

import random

import cocotb
import numpy as np

import sl_decode
import slmodel as M
from pecommon import NCH, SEED, start
from simrun import run
from streams import Stream, compare


def recording(rng, frames, kind):
    if kind == "quiet":
        x = rng.normal(0, 3, (frames, NCH))
    elif kind == "wild":                       # mostly escapes
        x = rng.uniform(-32768, 32767, (frames, NCH))
    else:                                      # channels of very different scale
        x = np.cumsum(rng.normal(0, 1, (frames, NCH)) * rng.choice([1, 20, 500, 5000], NCH), axis=0)
    return np.clip(np.round(x), -32768, 32767).astype(np.int64)


@cocotb.test()
async def matches_model_and_decodes(dut):
    rng = random.Random(SEED)
    data = np.random.default_rng(SEED)
    for kind, order, blog in [("quiet", 1, 2), ("wild", 0, 1), ("mixed", 2, 3), ("mixed", 1, 0)]:
        await start(dut)
        frames = 24 << blog if blog < 3 else 96
        x = recording(data, frames, kind)
        pred = M.Predictor(NCH, order, blog)
        e, first, lastblk = pred.run(x)
        want = M.Rice(NCH).run(e, first, lastblk)
        got = await Stream(dut, dut.clk, rng).run(M.Predictor.words(e, first, lastblk), len(want),
                                                  p_valid=0.8, p_ready=0.7)
        compare(got, want, f"{kind} order {order} block 2^{blog}")
        back = sl_decode.decode([w[0] for w in got], NCH, frames, blog, order)
        assert np.array_equal(back, x), f"{kind}: decoded samples differ"
        dut._log.info("%s: %d samples -> %d words (%.2f bits/sample)", kind, x.size, len(got),
                      32 * len(got) / x.size)


@cocotb.test()
async def escapes_take_two_cycles(dut):
    await start(dut)
    x = recording(np.random.default_rng(9), 32, "wild")
    e, first, lastblk = M.Predictor(NCH, 0, 5).run(x)
    s = Stream(dut, dut.clk, random.Random(9))
    want = M.Rice(NCH).run(e, first, lastblk)
    await s.run(M.Predictor.words(e, first, lastblk), len(want))
    gaps = np.diff(s.accept_cycle)
    assert gaps.max() <= 2, gaps.max()
    dut._log.info("worst case %d cycles per sample", gaps.max())


def test_pe_rice():
    run("pe_rice", ["pe_rice.sv"], "test_pe_rice", parameters={"NCH": NCH})
