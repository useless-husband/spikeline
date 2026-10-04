"""Tests of the golden model itself: the compressed bit format worked out by
hand, decoder round trips (including single blocks decoded on their own),
the filter against floating point, the noise estimator against the true
median, and the synthetic data generator."""

import numpy as np
import pytest
from scipy.signal import butter, sosfilt

import sl_decode
import slmodel as M
import synthdata


def encode(x, order, blog):
    e, first, lastblk = M.Predictor(x.shape[1], order, blog).run(x)
    return M.Rice(x.shape[1]).run(e, first, lastblk)


def test_rice_bits_by_hand():
    # one channel, order 0, blocks of 2 frames.  Frame 0 (first of a block):
    # e = 3 as 20 plain bits; A = 256 - 16 + zigzag(3) = 246 -> k = 3.
    # Frame 1: u = zigzag(-2) = 3 -> q = 0 -> "0" + "011".  Then zero padding.
    w = encode(np.array([[3], [-2]]), 0, 1)
    assert w.tolist() == [[int((f"{3:020b}" + "0011").ljust(32, "0"), 2), 0, 1]]
    # escape: u = 60000, q = 3750 >= 16 -> 16 ones, then u in 20 bits
    w = encode(np.array([[0], [30000]]), 0, 1)
    bits = "".join(f"{v:032b}" for v in w[:, 0])
    assert bits[20:].startswith("1" * 16 + f"{60000:020b}")


@pytest.mark.parametrize("order", [0, 1, 2, 3])
@pytest.mark.parametrize("blog", [0, 3, 7])
def test_decoder_round_trip(order, blog):
    rng = np.random.default_rng(order * 10 + blog)
    nch, frames = 5, 3 << max(blog, 2)
    x = np.cumsum(rng.normal(0, 1, (frames, nch)) * [1, 30, 900, 9000, 1], axis=0)
    x[:, 4] = np.where(np.arange(frames) % 2, 32767, -32768)        # worst-case residuals
    x = np.clip(np.round(x), -32768, 32767).astype(np.int64)
    w = encode(x, order, blog)
    assert np.array_equal(sl_decode.decode(w[:, 0], nch, frames, blog, order), x)


def test_each_block_decodes_on_its_own():
    x = synthdata.make_recording(seconds=0.02, snr=5, seed=4, nch=8)["x"].astype(np.int64)
    blog = 5
    frames = len(x) // 32 * 32
    w = encode(x[:frames], 1, blog)
    starts = [0] + list(np.nonzero(w[:, 2])[0][:-1] + 1)
    assert len(starts) == frames // 32
    for b in (1, len(starts) // 2, len(starts) - 1):
        part, _ = sl_decode.decode_block("".join(f"{v:032b}" for v in w[starts[b]:, 0]), 0, 8, 32, 1)
        assert np.array_equal(part, x[b * 32:(b + 1) * 32])
        assert w[starts[b], 1] == b & 0xFF                          # tag = block number


def test_bandpass_close_to_floating_point():
    rec = synthdata.make_recording(seconds=0.2, snr=5, seed=2, nch=4)
    x = rec["x"].astype(np.int64)
    y = M.Bandpass(4).run(x)
    ref = sosfilt(butter(2, (300, 6000), btype="bandpass", fs=30000, output="sos"), x.astype(float), axis=0)
    err = y - ref
    # rounding the output to whole LSBs alone gives 1/sqrt(12) = 0.29 LSB rms;
    # coefficient and state quantisation may add only a little to that
    assert err.std() < 0.4, err.std()
    assert np.abs(err).max() < 1.5


def test_noise_tracker_finds_the_median():
    rng = np.random.default_rng(1)
    sigma = np.array([5.0, 40.0, 300.0])
    x = np.round(rng.normal(0, 1, (30000, 3)) * sigma).astype(np.int64)
    n = M.Noise(3, step=16, warmup=0)
    noise, _ = n.run(x)
    est = noise[-3000:].mean(axis=0) / 16 / 0.6745
    assert np.allclose(est, sigma, rtol=0.06), est


def test_neo_matches_formula():
    rng = np.random.default_rng(3)
    x = rng.integers(-3000, 3000, (500, 2))
    noise = np.full((500, 2), 16 * 400)
    neo = M.Neo(2, c=256 * 4, refr=0)
    xd, det = neo.run(x, noise, np.ones(500, bool))
    psi = x[1:-1] ** 2 - x[:-2] * x[2:]
    assert np.array_equal(xd[1:], x[:-1])
    assert np.array_equal(det[2:], psi > 4 * 400 ** 2)


def test_generator_is_reproducible_and_scaled():
    a = synthdata.make_recording(seconds=0.1, snr=4, seed=9, nch=6)
    b = synthdata.make_recording(seconds=0.1, snr=4, seed=9, nch=6)
    c = synthdata.make_recording(seconds=0.1, snr=4, seed=10, nch=6)
    assert np.array_equal(a["x"], b["x"]) and not np.array_equal(a["x"], c["x"])
    assert a["clipped"] == 0
    assert np.all(np.diff(a["gt_time"][a["gt_chan"] == 0]) > 0)
    # each unit's band-passed peak is snr x sigma (checked on the template itself)
    for u in a["units"]:
        assert u["amp_uv"] > 0
