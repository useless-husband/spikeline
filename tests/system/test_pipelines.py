"""Whole-chip tests on the Verilator model: every output word of every port
is compared with the golden model, bit for bit."""

import numpy as np
import pytest

import chip as C
import sl_decode
import slmodel as M
import synthdata

NCH = 96
FAST = {"bp": 2500}            # functional tests: keep the band-pass from being the bottleneck
NOISE_FAST = M.cfg_noise(step=64, warmup=64)


@pytest.fixture(scope="module")
def rec():
    return synthdata.make_recording(seconds=0.04, snr=6, seed=11)["x"].astype(np.int64)


def golden_spike(x, noise_cfg=NOISE_FAST, neo_cfg=None):
    chip = M.Chip(NCH, noise_cfg=noise_cfg, neo_cfg=neo_cfg)
    return chip, chip.spike(x)


def test_register_map_and_counters(rec):
    x = rec[:4]
    s = C.Script().drain()
    s.expect(C.ID, 0x53504B4C).expect(C.PARAMS, (NCH << 24) | (7 << 16) | (9 << 8) | 0x3F)
    for d in C.DST:
        s.expect(0x10 + C.DST[d], 15)
    s.expect(0x20, 0).expect(0x21, M.cfg_noise()).expect(0x22, M.cfg_neo()).expect(0x24, M.cfg_predict())
    s.route("out2", "in").feed(4).drain()
    s.expect(C.IN_WORDS, 4 * NCH).expect(C.OUT_WORDS + 2, 4 * NCH).expect(C.OUT_WORDS, 0)
    res = C.run("regs", s, x)
    C.compare(res.port(2), M.input_words(x), "pass-through")


def test_spike_pipeline_with_probes(rec):
    s = C.Script().drain().pe_cfg("noise", NOISE_FAST, clear=True)
    s.routes(C.SPIKE + [("out1", "noise"), ("out2", "neo")]).feed(len(rec)).drain()
    res = C.run("spike", s, rec, periods=FAST, ready=(0.7, 0.8, 0.9), seed=2)
    _, g = golden_spike(rec)
    C.compare(res.port(1), M.Noise.words(g["filtered"], g["noise"], g["ready"]), "noise probe")
    C.compare(res.port(2), M.Neo.words(g["neo_x"], g["det"]), "NEO probe")
    C.compare(res.port(0), M.Snippet.words(g["packets"]), "snippets")
    assert len(g["packets"]) > 20


def test_compression_pipeline_is_lossless(rec):
    cfg = M.cfg_predict(order=2, block_log2=4)
    s = C.Script().drain().pe_cfg("pred", cfg, clear=True).routes(C.COMPRESS)
    frames = len(rec) // 16 * 16
    s.feed(frames).drain()
    res = C.run("compress", s, rec[:frames], periods=FAST, ready=(1, 0.5, 1), seed=3)
    chip = M.Chip(NCH, pred_cfg=cfg)
    want = chip.compress(rec[:frames])["words"]
    got = res.port(1)
    C.compare(got, want, "compressed words")
    back = sl_decode.decode(got[:, 0], NCH, frames, 4, 2)
    assert np.array_equal(back, rec[:frames]), "decoder output differs from the input"
