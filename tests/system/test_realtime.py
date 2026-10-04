"""Real-time regression tests: input paced like the ADC (one sample every
347.222 ns), the PE under test clocked 3 % above the 2.88 MHz it needs, and
no sample may arrive before the previous one was taken.

Both cases failed before the fixes recorded in CHANGELOG.md:
  * a clear written just before the input started (STATUS reported
    "drained" before the clear sweep had begun);
  * the Rice coder's burst at every block start (two-cycle escape codes)."""

import numpy as np

import chip as C
import slmodel as M
import synthdata

ADC_PS = 347222
SLOW = int(round(1e6 / (2.88 * 1.03)))     # 3 % above one sample per cycle
OTHERS = 20000                              # 50 MHz


def recording(frames):
    return synthdata.make_recording(frames / 30000 + 0.001, snr=5, seed=21)["x"][:frames].astype(np.int64)


def periods(pe):
    p = {k: OTHERS for k in C.CLOCKS}
    p["fab"] = 25000
    p[pe] = SLOW
    return p


def test_clear_then_feed_keeps_up():
    x = recording(600)
    s = C.Script().drain().pe_cfg("noise", M.cfg_noise(), clear=True).drain()
    s.routes([("noise", "in"), ("out0", "noise")]).feed(len(x)).drain()
    res = C.run("rt_clear", s, x, periods=periods("noise"), adc_period=ADC_PS, seed=3)
    assert res.stats["overruns"] == 0, res.stats


def test_rice_block_starts_keep_up():
    x = recording(1280)                     # ten blocks of 128 frames
    s = C.Script().drain().routes(C.COMPRESS).feed(len(x)).drain()
    res = C.run("rt_rice", s, x, periods=periods("rice"), adc_period=ADC_PS, seed=4)
    assert res.stats["overruns"] == 0, res.stats
    C.compare(res.port(1), M.Chip().compress(x)["words"], "compressed words")
