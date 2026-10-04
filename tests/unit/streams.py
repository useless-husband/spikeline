"""Valid/ready stream driver and sink for the cocotb tests.

Every edge: the driver offers a word with probability p_valid, the sink is
ready with probability p_ready (both from a seeded RNG, so a failure can be
replayed).  A word moves when valid and ready are both high at a rising edge;
the values are read in the ReadOnly phase just before that edge."""

from __future__ import annotations

import random

from cocotb.triggers import ReadOnly, RisingEdge


async def reset(dut, clk, rst, cycles=4):
    rst.value = 1
    for _ in range(cycles):
        await RisingEdge(clk)
    rst.value = 0


def pack(words):
    return [(int(d) & 0xFFFFFFFF, int(t), int(l)) for d, t, l in words]


class Stream:
    """Drives `<p>_valid/_data/_tag/_last` into a DUT and collects `<q>_*` out."""

    def __init__(self, dut, clk, rng: random.Random, inp="in", out="out"):
        self.dut, self.clk, self.rng = dut, clk, rng
        self.i = {s: getattr(dut, f"{inp}_{s}") for s in ("valid", "ready", "data", "tag", "last")}
        self.o = {s: getattr(dut, f"{out}_{s}") for s in ("valid", "ready", "data", "tag", "last")}
        self.received = []
        self.accept_cycle = []
        self.cycle = 0

    async def run(self, words, n_expect, p_valid=1.0, p_ready=1.0, max_cycles=2_000_000,
                  settle=200, stop_after=None):
        """Send `words`; return once n_expect words came out and `settle`
        further cycles passed without extra output (or fail after max_cycles).
        `stop_after(cycle)` may return True to pause input (used by tests)."""
        words = pack(words)
        i = 0
        quiet = 0
        self.i["valid"].value = 0
        self.o["ready"].value = 0
        await RisingEdge(self.clk)
        while True:
            v = i < len(words) and self.rng.random() < p_valid
            if stop_after is not None and stop_after(self.cycle):
                v = False
            if v:
                d, t, l = words[i]
                self.i["data"].value = d
                self.i["tag"].value = t
                self.i["last"].value = l
            self.i["valid"].value = int(v)
            r = self.rng.random() < p_ready
            self.o["ready"].value = int(r)
            await ReadOnly()
            in_fire = v and int(self.i["ready"].value) == 1
            out_fire = r and int(self.o["valid"].value) == 1
            if out_fire:
                self.received.append((int(self.o["data"].value), int(self.o["tag"].value),
                                      int(self.o["last"].value)))
            await RisingEdge(self.clk)
            self.cycle += 1
            if in_fire:
                self.accept_cycle.append(self.cycle)
                i += 1
            if i >= len(words) and len(self.received) >= n_expect:
                quiet += 1
                if quiet > settle:
                    break
            assert self.cycle < max_cycles, (
                f"timeout: sent {i}/{len(words)}, received {len(self.received)}/{n_expect}")
        self.i["valid"].value = 0
        return self.received


def compare(got, want, what=""):
    want = pack(want)
    assert len(got) == len(want), f"{what}: {len(got)} words out, expected {len(want)}"
    for n, (g, w) in enumerate(zip(got, want)):
        assert g == w, f"{what}: word {n}: got {g[0]:#010x}/{g[1]}/{g[2]}, want {w[0]:#010x}/{w[1]}/{w[2]}"
