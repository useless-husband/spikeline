"""Golden model of an arbitrary (meaningful) routing of the crossbar.

`evaluate(routes, x, cfgs)` runs the PEs that the routes connect, in data-
flow order, on framed arrays, and returns the exact word stream of every
crossbar source.  Allowed inputs: band-pass, noise and predictor take any
framed stream (they read data[15:0]); NEO reads the noise PE; snippet reads
NEO; Rice reads the predictor."""

from __future__ import annotations

import numpy as np

import slmodel as M

FRAMED = ("in", "bp", "noise", "neo", "pred")
ALLOWED = {"bp": FRAMED, "noise": FRAMED, "pred": FRAMED, "neo": ("noise",), "snip": ("neo",),
           "rice": ("pred",)}


def sext16(d):
    return ((np.asarray(d, dtype=np.int64) & 0xFFFF) ^ 0x8000) - 0x8000


class Golden:
    """Stateful: call run() once per phase, like the chip keeps PE state."""

    def __init__(self, nch, cfgs):
        self.nch = nch
        self.bp = M.Bandpass(nch)
        self.noise = M.Noise(nch)
        self.noise.configure(cfgs.get("noise", M.cfg_noise()))
        self.neo = M.Neo(nch)
        self.neo.configure(cfgs.get("neo", M.cfg_neo()))
        self.snip = M.Snippet(nch)
        self.pred = M.Predictor(nch)
        self.pred.configure(cfgs.get("pred", M.cfg_predict()))
        self.rice = M.Rice(nch)

    def run(self, routes: dict, x):
        """routes: destination -> source.  Returns source -> words array."""
        data = {"in": M.u32(x)}            # framed sources: data[F, C]
        words = {}
        todo = [d for d in ("bp", "noise", "pred", "neo", "snip", "rice") if routes.get(d)]
        while todo:
            for pe in list(todo):
                src = routes[pe]
                assert src in ALLOWED[pe], f"{pe} cannot read {src}"
                if src not in data:
                    continue
                d = data[src]
                if pe == "bp":
                    data["bp"] = M.u32(self.bp.run(sext16(d)))
                elif pe == "noise":
                    xs = sext16(d)
                    nz, rd = self.noise.run(xs)
                    data["noise"] = (rd.astype(np.int64)[:, None] << 31) | (nz << 16) | (xs & 0xFFFF)
                elif pe == "neo":
                    xd, det = self.neo.run(sext16(d), (d >> 16) & 0x7FFF, (d >> 31) & 1)
                    data["neo"] = (det.astype(np.int64) << 16) | (xd & 0xFFFF)
                elif pe == "pred":
                    e, first, lastblk = self.pred.run(sext16(d))
                    w = M.Predictor.words(e, first, lastblk)
                    data["pred"] = w[:, 0].reshape(d.shape)
                    self._pred_flags = (first, lastblk, e)
                elif pe == "snip":
                    words["snip"] = M.Snippet.words(self.snip.run(sext16(d), (d >> 16) & 1))
                elif pe == "rice":
                    first, lastblk, e = self._pred_flags
                    words["rice"] = self.rice.run(e, first, lastblk)
                todo.remove(pe)
        for k, d in data.items():
            words[k] = M.framed_words(d)
        return words
