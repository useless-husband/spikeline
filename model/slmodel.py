"""Bit-exact golden model of the spikeline processing elements (PEs).

Every PE of the chip has a class here that computes exactly the same
integers as the RTL, written as plainly as possible with numpy.  The input
of the chip is a time-multiplexed stream: frame f carries one 16-bit sample
for each of NCH channels in channel order (tag = channel, `last` on the final
channel).  The models therefore work on "framed" arrays of shape
[frames, channels]; the `*_words` helpers turn their results into the exact
32-bit stream words a PE emits, which is what the tests compare.

Stream word formats (data[31:0]; every word also has an 8-bit tag and a
`last` bit):

    raw sample      data = sign-extended x                tag = channel
    band-pass out   data = sign-extended y                tag = channel
    noise out       data = {ready, noise[14:0], x[15:0]}  tag = channel
    NEO out         data = {15'b0, det, x[15:0]}          tag = channel
    snippet out     header {peak frame}, then SNIP_LEN sign-extended samples,
                    tag = channel, last on the final sample
    predictor out   data = {lastblk, first, 10'b0, e[19:0]} tag = channel
    Rice out        packed bitstream, MSB first, tag = block number,
                    last on the final word of each block

All models are stateful (state survives between run() calls), like the
hardware, which keeps per-channel state across pipeline reconfiguration.
"""

from __future__ import annotations

import numpy as np

import sl_coefs

NCH = 96
FS_HZ = 30000

# ---------------------------------------------------------------- band-pass
F_STATE = 6        # fractional bits of the filter state
STATE_W = 24       # filter state width (18 integer bits: 4x headroom)
CF = sl_coefs.CF   # coefficient fractional bits

# ------------------------------------------------------------------- noise
STEP_DEFAULT = 16      # median tracker step, 1/256 LSB units (1/16 LSB)
WARMUP_DEFAULT = 1024  # frames before detections are allowed (34 ms)
M_W = 24               # tracker width, Q16.8

# --------------------------------------------------------------------- NEO
NEO_C_DEFAULT = 2700   # energy mode: threshold = noise^2 * C >> 16 (noise in 1/16 LSB)
AMP_C_DEFAULT = 1500   # amplitude mode: threshold = noise * C >> 12
REFR_DEFAULT = 30      # refractory period in frames (1 ms)
MODE_NEO, MODE_AMP = 0, 1

# ----------------------------------------------------------------- snippet
PRE = 10               # samples before the aligned peak
POST = 21              # samples after the aligned peak
ALIGN = 10             # peak search window (detection sample + 9 more)
RING_LOG2 = 5
SNIP_LEN = PRE + 1 + POST

# --------------------------------------------------- predictor + Rice coder
ORDER_DEFAULT = 1
BLOCK_LOG2_DEFAULT = 7  # 128 frames = 12288 samples per independently decodable block
A_INIT = 256            # adaptive-k accumulator reset value (mean |u| = 16)
A_W = 24
Q_ESC = 16              # unary length that signals an escape
RAW_BITS = 20           # escape payload width
K_MAX = 15

CLEAR_BIT = 1 << 31


def cfg_noise(step=STEP_DEFAULT, warmup=WARMUP_DEFAULT) -> int:
    return (step & 0xFF) | ((warmup & 0xFFFF) << 8)


def cfg_neo(c=NEO_C_DEFAULT, refr=REFR_DEFAULT, mode=MODE_NEO) -> int:
    return (c & 0xFFFF) | ((refr & 0xFF) << 16) | ((mode & 1) << 24)


def cfg_predict(order=ORDER_DEFAULT, block_log2=BLOCK_LOG2_DEFAULT) -> int:
    return (order & 3) | ((block_log2 & 0xF) << 2)


def sat(v, bits):
    """Signed saturation to `bits` bits."""
    return np.clip(v, -(1 << (bits - 1)), (1 << (bits - 1)) - 1)


def u32(v):
    return np.asarray(v, dtype=np.int64) & 0xFFFFFFFF


def framed_words(data, nch=None):
    """Words of a framed stream: data[F, C] -> (data, tag, last) arrays."""
    data = np.asarray(data, dtype=np.int64)
    f, c = data.shape
    tag = np.tile(np.arange(c, dtype=np.int64), f)
    last = np.tile(np.arange(c) == c - 1, f).astype(np.int64)
    return np.stack([u32(data.reshape(-1)), tag, last], axis=1)


def input_words(x):
    return framed_words(x)


# =========================================================== band-pass PE
class Bandpass:
    """Two direct-form-I biquads; one multiply-accumulate unit in hardware.

    Section input u and output v are Q18.6 integers (STATE_W bits).  The
    accumulator is exact (48 bits); each section rounds once (add half,
    arithmetic shift right by CF) and saturates to STATE_W bits.  The final
    output rounds away the F_STATE fractional bits and saturates to 16 bits.
    """

    def __init__(self, nch=NCH):
        self.nch = nch
        self.st = np.zeros((6, nch), dtype=np.int64)  # x1 x2 y1a y2a y1b y2b

    def run(self, x):
        x = np.asarray(x, dtype=np.int64)
        (b00, b01, b02, na01, na02), (b10, b11, b12, na11, na12) = sl_coefs.SECTIONS
        x1, x2, y1a, y2a, y1b, y2b = (s.copy() for s in self.st)
        half_c = 1 << (CF - 1)
        half_s = 1 << (F_STATE - 1)
        y = np.empty_like(x)
        for f in range(x.shape[0]):
            xf = x[f]
            acc = (b00 * (xf << F_STATE) + b01 * (x1 << F_STATE) + b02 * (x2 << F_STATE)
                   + na01 * y1a + na02 * y2a)
            v0 = sat((acc + half_c) >> CF, STATE_W)
            acc = b10 * v0 + b11 * y1a + b12 * y2a + na11 * y1b + na12 * y2b
            v1 = sat((acc + half_c) >> CF, STATE_W)
            y[f] = sat((v1 + half_s) >> F_STATE, 16)
            x2, x1 = x1, xf
            y2a, y1a = y1a, v0
            y2b, y1b = y1b, v1
        self.st = np.stack([x1, x2, y1a, y2a, y1b, y2b])
        return y

    @staticmethod
    def words(y):
        return framed_words(y)


# ========================================================= noise estimator
class Noise:
    """Per-channel running median of |x| by sign tracking.

    m (Q16.8) moves up by `step` when |x| is above it and down when below,
    so it settles where half the samples are above: the median of |x|.  For
    Gaussian noise sigma = median(|x|) / 0.6745 (the usual MAD estimator);
    the 1/0.6745^2 factor is folded into the NEO threshold constant.  Spikes
    are rare, so they barely move a median, unlike a running RMS.

    Emits, with each sample, noise = m >> 4 (Q11.4, 15 bits, the value
    *before* this sample's update) and ready = (frames seen >= warmup).
    """

    def __init__(self, nch=NCH, step=STEP_DEFAULT, warmup=WARMUP_DEFAULT):
        self.nch, self.step, self.warmup = nch, step, warmup
        self.m = np.zeros(nch, dtype=np.int64)
        self.fcnt = 0

    def configure(self, cfg):
        self.step = cfg & 0xFF
        self.warmup = (cfg >> 8) & 0xFFFF

    def run(self, x):
        x = np.asarray(x, dtype=np.int64)
        noise = np.empty_like(x)
        ready = np.empty(x.shape[0], dtype=bool)
        mmax = (1 << M_W) - 1
        m = self.m.copy()
        for f in range(x.shape[0]):
            noise[f] = np.minimum(m >> 4, (1 << 15) - 1)
            ready[f] = self.fcnt >= self.warmup
            a = np.abs(x[f]) << 8
            m = np.where(a > m, np.minimum(m + self.step, mmax),
                         np.where(a < m, np.maximum(m - self.step, 0), m))
            self.fcnt = min(self.fcnt + 1, 0xFFFF)
        self.m = m
        return noise, ready

    @staticmethod
    def words(x, noise, ready):
        x = np.asarray(x, dtype=np.int64)
        data = (np.asarray(ready, dtype=np.int64)[:, None] << 31) | (noise << 16) | (x & 0xFFFF)
        return framed_words(data)


# ============================================================ NEO detector
class Neo:
    """Nonlinear energy operator psi[n] = x[n]^2 - x[n-1] x[n+1].

    psi needs the next sample, so the output for x[n] leaves when x[n+1]
    arrives: the stream comes out one frame late, with an all-zero first
    frame.  A detection needs ready, psi > (noise^2 * C) >> 16, and the
    channel out of its refractory period (`refr` frames after a detection).
    Mode 1 replaces the energy test by a plain amplitude test,
    |x[n]| > (noise * C) >> 12, for comparison.
    """

    def __init__(self, nch=NCH, c=NEO_C_DEFAULT, refr=REFR_DEFAULT, mode=MODE_NEO):
        self.nch, self.c, self.refr, self.mode = nch, c, refr, mode
        self.xp = np.zeros(nch, dtype=np.int64)
        self.xc = np.zeros(nch, dtype=np.int64)
        self.r = np.zeros(nch, dtype=np.int64)

    def configure(self, cfg):
        self.c = cfg & 0xFFFF
        self.refr = (cfg >> 16) & 0xFF
        self.mode = (cfg >> 24) & 1

    def run(self, x, noise, ready):
        x = np.asarray(x, dtype=np.int64)
        out = np.empty_like(x)
        det = np.empty(x.shape, dtype=bool)
        xp, xc, r = self.xp, self.xc, self.r
        for f in range(x.shape[0]):
            if self.mode == MODE_NEO:
                hit = xc * xc - xp * x[f] > (noise[f] * noise[f] * self.c) >> 16
            else:
                hit = np.abs(xc) > (noise[f] * self.c) >> 12
            d = np.asarray(ready[f], dtype=bool) & hit & (r == 0)   # ready: per frame or per word
            out[f] = xc
            det[f] = d
            r = np.where(d, self.refr, np.maximum(r - 1, 0))
            xp, xc = xc, x[f]
        self.xp, self.xc, self.r = xp, xc, r
        return out, det

    @staticmethod
    def words(xd, det):
        return framed_words((np.asarray(det, dtype=np.int64) << 16) | (np.asarray(xd) & 0xFFFF))


# ======================================================== snippet extractor
class Snippet:
    """Cut a SNIP_LEN-sample window around each detected spike's peak.

    After a detection the peak (largest |x|, first one on ties) is searched
    over ALIGN samples starting at the detection; the window
    [peak - PRE, peak + POST] is sent once peak + POST has arrived, as a
    header word (frame number of the peak, counted at this PE's input) and
    SNIP_LEN sample words.  Detections in frames < PRE, or while the channel
    is already capturing, are ignored.
    """

    def __init__(self, nch=NCH):
        self.nch = nch
        self.ring = np.zeros((nch, 1 << RING_LOG2), dtype=np.int64)
        self.active = np.zeros(nch, dtype=bool)
        self.srch = np.zeros(nch, dtype=np.int64)
        self.post = np.zeros(nch, dtype=np.int64)
        self.peak = np.zeros(nch, dtype=np.int64)
        self.fcnt = 0

    def run(self, x, det):
        x = np.asarray(x, dtype=np.int64)
        det = np.asarray(det, dtype=bool)
        mask = (1 << RING_LOG2) - 1
        packets = []
        for f in range(x.shape[0]):
            fc = self.fcnt
            self.ring[:, fc & mask] = x[f]
            a = np.abs(x[f])
            act = self.active
            start = ~act & det[f] & (fc >= PRE)
            searching = act & (self.srch > 0)
            better = searching & (a > self.peak)
            self.peak = np.where(start | better, a, self.peak)
            self.post = np.where(start | better, POST, np.where(act, self.post - 1, self.post))
            self.srch = np.where(start, ALIGN - 1, np.where(searching, self.srch - 1, self.srch))
            self.active = act | start
            emit = self.active & (self.post == 0) & (self.srch == 0)
            for ch in np.nonzero(emit)[0]:
                idx = (fc - (SNIP_LEN - 1) + np.arange(SNIP_LEN)) & mask
                packets.append((int(ch), (fc - POST) & 0xFFFFFFFF, self.ring[ch, idx].copy()))
            self.active &= ~emit
            self.fcnt = (self.fcnt + 1) & 0xFFFFFFFF
        return packets

    @staticmethod
    def words(packets):
        rows = []
        for ch, peak_frame, samples in packets:
            rows.append((peak_frame, ch, 0))
            for i, s in enumerate(samples):
                rows.append((int(s) & 0xFFFFFFFF, ch, int(i == SNIP_LEN - 1)))
        return np.array(rows, dtype=np.int64).reshape(-1, 3)


# ===================================================== linear predictor
class Predictor:
    """Per-channel fixed linear prediction, restarted every 2^block_log2 frames.

    e = x (order 0), x - x[n-1] (order 1) or x - 2x[n-1] + x[n-2] (order 2).
    The order used in frame fb of a block is min(order, fb), so a block never
    looks back into the previous one and can be decoded on its own.
    """

    def __init__(self, nch=NCH, order=ORDER_DEFAULT, block_log2=BLOCK_LOG2_DEFAULT):
        self.nch, self.order, self.block_log2 = nch, order, block_log2
        self.x1 = np.zeros(nch, dtype=np.int64)
        self.x2 = np.zeros(nch, dtype=np.int64)
        self.fb = 0

    def configure(self, cfg):
        self.order = cfg & 3
        self.block_log2 = (cfg >> 2) & 0xF

    def run(self, x):
        x = np.asarray(x, dtype=np.int64)
        e = np.empty_like(x)
        first = np.empty(x.shape[0], dtype=bool)
        lastblk = np.empty(x.shape[0], dtype=bool)
        mask = (1 << self.block_log2) - 1
        order = min(self.order, 2)
        for f in range(x.shape[0]):
            eff = min(order, self.fb)
            if eff == 0:
                e[f] = x[f]
            elif eff == 1:
                e[f] = x[f] - self.x1
            else:
                e[f] = x[f] - 2 * self.x1 + self.x2
            first[f] = self.fb == 0
            lastblk[f] = self.fb >= mask
            self.x2, self.x1 = self.x1, x[f]
            self.fb = 0 if self.fb >= mask else self.fb + 1
        return e, first, lastblk

    @staticmethod
    def words(e, first, lastblk):
        e = np.asarray(e, dtype=np.int64)
        f, c = e.shape
        flags = np.zeros((f, c), dtype=np.int64)
        flags |= np.asarray(first, dtype=np.int64)[:, None] << 30
        flags[:, c - 1] |= np.asarray(lastblk, dtype=np.int64) << 31
        return framed_words(flags | (e & 0xFFFFF))


# ======================================================== adaptive Rice coder
def floor_log2(m):
    m = np.asarray(m, dtype=np.int64)
    out = np.zeros_like(m)
    for b in range(1, 32):
        out = np.where(m >= (1 << b), b, out)
    return out


class Rice:
    """Adaptive Rice coding of residuals, packed MSB-first into 32-bit words.

    Per channel, A tracks 16 x the running mean of u = zigzag(e)
    (A <- A - A/16 + u).  k = floor(log2(A >> 4)) (0 if that is 0), at most
    K_MAX.  Code for u: q = u >> k ones, a zero, then the low k bits of u.
    If q >= Q_ESC: Q_ESC ones then u in RAW_BITS bits.  A restarts from
    A_INIT at the first frame of a block, where the predictor sends the
    samples themselves; those are sent as RAW_BITS plain bits (but still
    update A).  After the last
    sample of a block the packer pads to a word boundary and marks that word
    `last`.
    """

    def __init__(self, nch=NCH):
        self.nch = nch
        self.a = np.zeros(nch, dtype=np.int64)
        self.carry = np.zeros(0, dtype=np.uint8)  # bits not yet in a full word
        self.seq = 0

    def _pieces(self, e, first):
        """(value, length) code pieces for one frame, two pieces per sample."""
        e = ((np.asarray(e, dtype=np.int64) & 0xFFFFF) ^ 0x80000) - 0x80000  # sign-extend 20 bits
        a = np.full(self.nch, A_INIT, dtype=np.int64) if first else self.a
        u = np.where(e >= 0, 2 * e, -2 * e - 1)
        if first:
            # first frame of a block: residual sent as 20 plain bits; it still
            # updates A, which gives the adaptation a data-driven start
            self.a = a - (a >> 4) + u
            zero = np.zeros(self.nch, dtype=np.int64)
            return (np.stack([e & 0xFFFFF, zero], 1).reshape(-1),
                    np.stack([np.full(self.nch, RAW_BITS), zero], 1).reshape(-1))
        k = np.minimum(floor_log2(a >> 4), K_MAX)
        q = u >> k
        esc = q >= Q_ESC
        v1 = np.where(esc, (1 << Q_ESC) - 1, (((1 << q) - 1) << (k + 1)) | (u & ((1 << k) - 1)))
        l1 = np.where(esc, Q_ESC, q + 1 + k)
        v2 = np.where(esc, u, 0)
        l2 = np.where(esc, RAW_BITS, 0)
        self.a = a - (a >> 4) + u
        return np.stack([v1, v2], 1).reshape(-1), np.stack([l1, l2], 1).reshape(-1)

    @staticmethod
    def _bits(vals, lens):
        keep = lens > 0
        vals, lens = vals[keep], lens[keep]
        total = int(lens.sum())
        piece = np.repeat(np.arange(len(lens)), lens)
        start = np.cumsum(lens) - lens
        j = np.arange(total) - start[piece]
        return ((vals[piece] >> (lens[piece] - 1 - j)) & 1).astype(np.uint8)

    def run(self, e, first, lastblk, chunk=512):
        """Returns (data, tag, last) rows of the words completed by this call."""
        e = np.asarray(e, dtype=np.int64)
        rows = []
        f0 = 0
        nf = e.shape[0]
        while f0 < nf:
            # a segment ends after a block-final frame or after `chunk` frames
            f1 = f0
            while f1 < nf and f1 - f0 < chunk and not lastblk[f1]:
                f1 += 1
            flush = f1 < nf and bool(lastblk[f1])
            end = f1 + 1 if flush else f1
            vs, ls = [], []
            for f in range(f0, end):
                v, ln = self._pieces(e[f], bool(first[f]))
                vs.append(v)
                ls.append(ln)
            bits = self.carry
            if vs:
                bits = np.concatenate([bits, self._bits(np.concatenate(vs), np.concatenate(ls))])
            if flush and len(bits) % 32:
                bits = np.concatenate([bits, np.zeros(32 - len(bits) % 32, dtype=np.uint8)])
            nfull = len(bits) // 32
            if nfull:
                words = np.packbits(bits[:nfull * 32]).view(">u4").astype(np.int64)
                last = np.zeros(nfull, dtype=np.int64)
                if flush:
                    last[-1] = 1
                rows.append(np.stack([words, np.full(nfull, self.seq & 0xFF), last], 1))
            self.carry = bits[nfull * 32:]
            if flush:
                self.seq += 1
            f0 = end
        if not rows:
            return np.zeros((0, 3), dtype=np.int64)
        return np.concatenate(rows)


# ============================================================== pipelines
class Chip:
    """All six PEs with their state, for driving whole pipelines."""

    def __init__(self, nch=NCH, noise_cfg=None, neo_cfg=None, pred_cfg=None):
        self.bp = Bandpass(nch)
        self.noise = Noise(nch)
        self.neo = Neo(nch)
        self.snip = Snippet(nch)
        self.pred = Predictor(nch)
        self.rice = Rice(nch)
        if noise_cfg is not None:
            self.noise.configure(noise_cfg)
        if neo_cfg is not None:
            self.neo.configure(neo_cfg)
        if pred_cfg is not None:
            self.pred.configure(pred_cfg)

    def spike(self, x):
        """filter -> noise -> NEO -> snippet; returns every intermediate stream."""
        y = self.bp.run(x)
        noise, ready = self.noise.run(y)
        xd, det = self.neo.run(y, noise, ready)
        packets = self.snip.run(xd, det)
        return {"filtered": y, "noise": noise, "ready": ready, "neo_x": xd, "det": det,
                "packets": packets}

    def compress(self, x):
        e, first, lastblk = self.pred.run(x)
        return {"e": e, "first": first, "lastblk": lastblk, "words": self.rice.run(e, first, lastblk)}
