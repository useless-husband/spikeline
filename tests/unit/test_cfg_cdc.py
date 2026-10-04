"""Configuration handshake across clock domains: every word written on the
fabric side arrives intact on the PE side, in order, exactly once, and a
set bit 31 becomes exactly one clear pulse.  A fake PE goes busy for a few
cycles after each clear, and the fabric side must stay busy until that
sweep is over.  Unrelated clocks, random phase, CDC jitter injection on."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from simrun import run

RATIOS = [(10000, 10000), (10000, 33000), (33000, 10000), (5000, 97000), (97000, 5000)]


async def watch_b(dut, seen, clears, stop, sweep=7):
    """Records cfg changes and clear pulses; plays a PE that clears for `sweep` cycles."""
    last = None
    busy_left = 0
    prev_clr = 0
    while not stop["done"]:
        await RisingEdge(dut.clk_b)
        busy_left = sweep if prev_clr else max(busy_left - 1, 0)   # reacts to the clr of the last edge
        dut.pe_idle.value = int(busy_left == 0)
        stop["pe_busy"] = busy_left > 0
        await ReadOnly()
        v = int(dut.cfg.value)
        if v != last:
            seen.append(v)
            last = v
        prev_clr = int(dut.clr.value)
        if prev_clr:
            clears.append(len(seen))


@cocotb.test()
async def every_word_arrives_once(dut):
    rng = random.Random(5)
    for ta, tb in RATIOS:
        ca = cocotb.start_soon(Clock(dut.clk_a, ta, unit="ps").start())
        await Timer(rng.randrange(1, tb), unit="ps")
        cb = cocotb.start_soon(Clock(dut.clk_b, tb, unit="ps").start())
        dut.wr.value = 0
        dut.rst_a.value = 1
        dut.rst_b.value = 1
        for _ in range(3):
            await RisingEdge(dut.clk_a)
            await RisingEdge(dut.clk_b)
        dut.rst_a.value = 0
        dut.rst_b.value = 0
        dut.pe_idle.value = 1
        seen, clears, stop = [], [], {"done": False}
        w = cocotb.start_soon(watch_b(dut, seen, clears, stop))
        # distinct consecutive values so every update is visible on side b
        words = []
        for i in range(40):
            words.append(((i + 1) * 0x01010101 & 0x7FFFFFFF) | (rng.random() < 0.3) << 31)
        sent = 0
        while sent < len(words):
            await RisingEdge(dut.clk_a)
            dut.wr.value = 0
            if rng.random() < 0.5:
                continue
            await ReadOnly()
            busy = int(dut.busy.value)
            assert busy or not stop.get("pe_busy"), "fabric side not busy while the PE is still clearing"
            await RisingEdge(dut.clk_a)
            if not busy:
                dut.wr.value = 1
                dut.wdata.value = words[sent]
                sent += 1
        await RisingEdge(dut.clk_a)
        dut.wr.value = 0
        for _ in range(12):
            await RisingEdge(dut.clk_b)
            await RisingEdge(dut.clk_a)
        stop["done"] = True
        await w
        await Timer(1, unit="ps")   # leave the ReadOnly phase the watcher ended in
        want = [w_ & 0x7FFFFFFF for w_ in words]
        assert seen[1:] == want, f"{ta}/{tb} ps: config words lost, repeated or corrupted"
        want_clears = [i + 1 for i, w_ in enumerate(words) if w_ >> 31]
        assert [c - 1 for c in clears] == want_clears, f"{ta}/{tb} ps: clear pulses wrong"
        ca.cancel()
        cb.cancel()


def test_cfg_cdc():
    run("sl_cfg_cdc", ["sl_sync2.sv", "sl_cfg_cdc.sv"], "test_cfg_cdc", parameters={"ASYNC": 1},
        defines={"SL_CDC_JITTER": 1})
