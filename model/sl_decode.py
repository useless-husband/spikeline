"""Software decoder for the compressed stream (predictor + adaptive Rice).

Written separately from the encoder model on purpose: it reads the bits the
hardware produced and rebuilds the raw samples, so `decode(encode(x)) == x`
is a real check of losslessness rather than the encoder checking itself.

Stream layout (see docs/report.md, section "Compression"): blocks of
2^block_log2 frames x nch samples, each block starting on a 32-bit word
boundary.  First frame of a block: each sample as 20 plain bits.  Other
samples: q ones, a zero, k remainder bits (k from the channel's running
mean, reset at every block), or Q_ESC ones followed by a 20-bit raw value.  The residual is zigzag-coded and added to a fixed
linear prediction of order min(order, frame index within the block).
"""

from __future__ import annotations

import numpy as np

Q_ESC = 16
RAW_BITS = 20
A_INIT = 256
K_MAX = 15


def _k(a: int) -> int:
    m = a >> 4
    return min(m.bit_length() - 1, K_MAX) if m > 0 else 0


def decode_block(bits: str, pos: int, nch: int, nframes: int, order: int):
    """Decode one block starting at bit `pos`; returns (samples[nframes][nch], end_pos)."""
    acc = [A_INIT] * nch
    x1 = [0] * nch
    x2 = [0] * nch
    out = np.zeros((nframes, nch), dtype=np.int64)
    for fb in range(nframes):
        eff = min(order, fb)
        row = out[fb]
        for c in range(nch):
            k = _k(acc[c])
            if fb == 0:                       # first frame: plain 20-bit two's complement
                v = int(bits[pos:pos + RAW_BITS], 2)
                pos += RAW_BITS
                e = v - (1 << RAW_BITS) if v >> (RAW_BITS - 1) else v
                row[c] = e
                x2[c] = x1[c]
                x1[c] = e
                continue
            if bits.startswith("1" * Q_ESC, pos):
                pos += Q_ESC
                u = int(bits[pos:pos + RAW_BITS], 2)
                pos += RAW_BITS
            else:
                z = bits.index("0", pos)
                q = z - pos
                pos = z + 1
                r = int(bits[pos:pos + k], 2) if k else 0
                pos += k
                u = (q << k) | r
            acc[c] = acc[c] - (acc[c] >> 4) + u
            e = (u >> 1) ^ -(u & 1)
            if eff == 0:
                v = e
            elif eff == 1:
                v = e + x1[c]
            else:
                v = e + 2 * x1[c] - x2[c]
            row[c] = v
            x2[c] = x1[c]
            x1[c] = v
    return out, pos


def decode(words, nch: int, nframes: int, block_log2: int, order: int) -> np.ndarray:
    """Decode a whole stream of 32-bit words into samples[nframes][nch]."""
    bits = "".join(f"{int(w) & 0xFFFFFFFF:032b}" for w in words)
    order = min(order, 2)
    blk = 1 << block_log2
    out = []
    pos = 0
    done = 0
    while done < nframes:
        n = min(blk, nframes - done)
        part, pos = decode_block(bits, pos, nch, n, order)
        out.append(part)
        done += n
        pos = (pos + 31) // 32 * 32  # next block starts on a word boundary
    return np.concatenate(out) if out else np.zeros((0, nch), dtype=np.int64)
