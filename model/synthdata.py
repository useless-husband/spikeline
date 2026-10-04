"""Synthetic extracellular recordings with known spike times.

A recording is built in microvolts and quantised like a 16-bit neural
amplifier with 0.195 uV per LSB (the step of common headstage ADCs):

    raw = DC offset + field potential (1-40 Hz)              (removed by the filter)
        + amplifier noise (white) + biological 1/f noise
        + background multi-unit activity (many small far-away spikes)
        + single units (the spikes we want to find, with ground truth)

Each channel has 1-3 units.  A unit has its own biphasic waveform
(negative trough, slower positive rebound, ~1.6 ms), fires as a Poisson
process (3-25 Hz) with a 2 ms refractory period, and is scaled so that the
peak of its *band-passed* waveform is `snr` times the standard deviation of
the band-passed background (everything except the units) on that channel.
That is the usual definition of SNR in spike-detection papers.

Ground truth time of a spike = sample where its band-passed waveform has the
largest magnitude, i.e. where an aligned detector should put the peak.
Everything is drawn from numpy's PCG64 with the given seed.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt

FS_HZ = 30000
LSB_UV = 0.195
TEMPLATE_LEN = 48          # 1.6 ms
TROUGH_AT = 12             # index of the trough inside the template


def bandpass_float(x):
    sos = butter(2, (300, 6000), btype="bandpass", fs=FS_HZ, output="sos")
    return sosfilt(sos, x, axis=0)


def unit_template(rng):
    """One biphasic spike waveform, peak magnitude 1 (negative trough)."""
    t = (np.arange(TEMPLATE_LEN) - TROUGH_AT) / FS_HZ * 1e3          # ms
    w1 = rng.uniform(0.07, 0.14)                                     # trough width
    t2 = rng.uniform(0.25, 0.5)                                      # rebound delay
    w2 = rng.uniform(0.18, 0.35)                                     # rebound width
    r = rng.uniform(0.2, 0.5)                                        # rebound size
    w = -np.exp(-t ** 2 / (2 * w1 ** 2)) + r * np.exp(-(t - t2) ** 2 / (2 * w2 ** 2))
    w *= np.clip((t + 0.4) / 0.1, 0, 1)                              # clean onset
    if rng.random() < 0.15:                                          # some units are positive
        w = -w
    return w / np.max(np.abs(w))


def _poisson_train(rng, n, rate_hz, refractory):
    times = []
    t = 0.0
    while True:
        t += refractory + rng.exponential(FS_HZ / rate_hz)
        if t >= n - TEMPLATE_LEN:
            return np.array(times, dtype=np.int64)
        times.append(int(t))


def make_recording(seconds=1.0, snr=5.0, seed=1, nch=96):
    """Returns a dict with x (int16 [samples, nch]) and the ground truth."""
    rng = np.random.default_rng(seed)
    n = int(round(seconds * FS_HZ))
    tt = np.arange(n) / FS_HZ

    # background: everything that is not a tracked unit
    white = rng.normal(0.0, 3.0, (n, nch))
    spec = np.fft.rfft(rng.normal(0.0, 1.0, (n, nch)), axis=0)
    freqs = np.fft.rfftfreq(n, 1 / FS_HZ)
    shape = np.where(freqs > 1.0, 1.0 / np.sqrt(np.maximum(freqs, 1.0)), 0.0)
    pink = np.fft.irfft(spec * shape[:, None], n=n, axis=0)
    pink *= 25.0 / (pink.std(axis=0, keepdims=True) + 1e-12)        # 25 uV rms, mostly low frequency
    background = white + pink

    # multi-unit activity: many small spikes from distant neurons
    bank = [unit_template(rng) for _ in range(12)]
    mua_rate = 400.0
    for c in range(nch):
        k = rng.poisson(mua_rate * seconds)
        starts = rng.integers(0, n - TEMPLATE_LEN, k)
        amps = rng.uniform(3.0, 9.0, k)
        which = rng.integers(0, len(bank), k)
        idx = starts[:, None] + np.arange(TEMPLATE_LEN)
        np.add.at(background[:, c], idx.reshape(-1),
                  (amps[:, None] * np.stack([bank[w] for w in which])).reshape(-1)
                  if k else np.zeros(0))

    sigma = bandpass_float(background).std(axis=0)                  # uV, per channel

    # field potential and offset (not part of the band, so not part of sigma)
    lfp = np.zeros((n, nch))
    common = sum(rng.uniform(30, 80) * np.sin(2 * np.pi * rng.uniform(1, 12) * tt + rng.uniform(0, 6.3))
                 for _ in range(3))
    for c in range(nch):
        lfp[:, c] = common * rng.uniform(0.6, 1.0) + sum(
            rng.uniform(5, 30) * np.sin(2 * np.pi * rng.uniform(4, 40) * tt + rng.uniform(0, 6.3))
            for _ in range(3))
    offset = rng.uniform(-300.0, 300.0, nch)

    # tracked units
    signal = np.zeros((n, nch))
    gt_t, gt_c, gt_u, units = [], [], [], []
    pad = np.zeros(TEMPLATE_LEN + 200)
    for c in range(nch):
        for _ in range(rng.integers(1, 4)):
            w = unit_template(rng)
            pad[:] = 0
            pad[50:50 + TEMPLATE_LEN] = w
            fw = bandpass_float(pad)
            peak_at = int(np.argmax(np.abs(fw))) - 50                # offset from spike start
            amp = snr * sigma[c] / np.max(np.abs(fw))
            starts = _poisson_train(rng, n, rng.uniform(3, 25), 0.002 * FS_HZ)
            if len(starts):
                idx = starts[:, None] + np.arange(TEMPLATE_LEN)
                np.add.at(signal[:, c], idx.reshape(-1), np.tile(amp * w, len(starts)))
            uid = len(units)
            units.append({"channel": c, "amp_uv": float(amp), "peak_offset": peak_at})
            gt_t.extend((starts + peak_at).tolist())
            gt_c.extend([c] * len(starts))
            gt_u.extend([uid] * len(starts))

    total = offset + lfp + background + signal
    q = np.round(total / LSB_UV)
    clipped = int(np.count_nonzero((q < -32768) | (q > 32767)))
    x = np.clip(q, -32768, 32767).astype(np.int16)
    order = np.lexsort((np.array(gt_t), np.array(gt_c)))
    return {
        "x": x,
        "gt_time": np.array(gt_t, dtype=np.int64)[order],
        "gt_chan": np.array(gt_c, dtype=np.int64)[order],
        "gt_unit": np.array(gt_u, dtype=np.int64)[order],
        "units": units,
        "sigma_lsb": sigma / LSB_UV,
        "clipped": clipped,
        "snr": snr,
        "seed": seed,
    }


def match(det_time, det_chan, gt_time, gt_chan, tol, t_lo=0, t_hi=None):
    """One-to-one matching of detections to ground truth on the same channel.

    Both lists are restricted to times in [t_lo, t_hi).  A detection matches
    the earliest unmatched true spike within +-tol samples.  Returns
    (true positives, false positives, false negatives)."""
    det_time, det_chan = np.asarray(det_time), np.asarray(det_chan)
    gt_time, gt_chan = np.asarray(gt_time), np.asarray(gt_chan)
    hi = np.inf if t_hi is None else t_hi
    dsel = (det_time >= t_lo) & (det_time < hi)
    gsel = (gt_time >= t_lo) & (gt_time < hi)
    tp = fp = fn = 0
    for c in np.union1d(det_chan[dsel], gt_chan[gsel]):
        d = np.sort(det_time[dsel & (det_chan == c)])
        g = np.sort(gt_time[gsel & (gt_chan == c)])
        used = np.zeros(len(g), dtype=bool)
        j0 = 0
        for t in d:
            while j0 < len(g) and g[j0] < t - tol:
                j0 += 1
            j = j0
            hit = False
            while j < len(g) and g[j] <= t + tol:
                if not used[j]:
                    used[j] = True
                    hit = True
                    break
                j += 1
            if hit:
                tp += 1
            else:
                fp += 1
        fn += int(np.count_nonzero(~used))
    return tp, fp, fn
