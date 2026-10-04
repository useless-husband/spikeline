"""Asynchronous FIFO stress test.

Writer and reader run on unrelated clocks (ratios from 1:1 to 1:30 in both
directions, including two clocks 0.1% apart whose phase drifts slowly),
with a random phase offset and random valid/ready gaps.  The synchronisers
are built with SL_CDC_JITTER, so every pointer bit that changes may arrive
one cycle late at random, as metastability can make it do in silicon.

Checked: every word arrives exactly once and in order; the writer never
sees ready while the FIFO holds 2^AW words; the reader never sees valid on
an empty FIFO (it would read a stale word and fail the order check)."""

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

from simrun import run

AW = int(os.environ.get("TB_AW", "3"))
SEED = int(os.environ.get("TB_SEED", "1"))
# (write period, read period) in ps
RATIOS = [(10000, 10000), (10000, 10010), (10000, 37000), (37000, 10000), (7000, 23000),
          (3000, 90000), (90000, 3000)]


async def writer(dut, items, rng, p_valid, state):
    i = 0
    while i < len(items):
        v = rng.random() < p_valid
        dut.w_valid.value = int(v)
        dut.w_data.value = items[i]
        await ReadOnly()
        fire = v and int(dut.w_ready.value) == 1
        if int(dut.w_ready.value):
            assert state["writes"] - state["reads"] < (1 << AW), "ready while full"
        await RisingEdge(dut.wclk)
        if fire:
            state["writes"] += 1
            i += 1
    dut.w_valid.value = 0


async def reader(dut, n, rng, p_ready, state, got):
    while len(got) < n:
        r = rng.random() < p_ready
        dut.r_ready.value = int(r)
        await ReadOnly()
        if r and int(dut.r_valid.value):
            got.append(int(dut.r_data.value))
            state["reads"] += 1
        await RisingEdge(dut.rclk)
    dut.r_ready.value = 0


@cocotb.test()
async def stress_all_ratios(dut):
    rng = random.Random(SEED)
    for tw, tr in RATIOS:
        for p_valid, p_ready in [(1.0, 1.0), (0.9, 0.3), (0.3, 0.9), (0.6, 0.6)]:
            await one_run(dut, rng, tw, tr, p_valid, p_ready)
    dut._log.info("all clock ratios passed (AW=%d, seed %d)", AW, SEED)


async def one_run(dut, rng, tw, tr, p_valid, p_ready, n=400):
    wclk = cocotb.start_soon(Clock(dut.wclk, tw, unit="ps").start())
    await Timer(rng.randrange(1, tr), unit="ps")                  # random phase
    rclk = cocotb.start_soon(Clock(dut.rclk, tr, unit="ps").start())
    dut.w_valid.value = 0
    dut.r_ready.value = 0
    dut.wrst.value = 1
    dut.rrst.value = 1
    for _ in range(3):
        await RisingEdge(dut.wclk)
        await RisingEdge(dut.rclk)
    dut.wrst.value = 0
    dut.rrst.value = 0
    items = [rng.getrandbits(41) for _ in range(n)]
    state = {"writes": 0, "reads": 0}
    got = []
    w = cocotb.start_soon(writer(dut, items, random.Random(rng.random()), p_valid, state))
    await reader(dut, n, random.Random(rng.random()), p_ready, state, got)
    await w
    assert got == items, f"periods {tw}/{tr} ps: data out of order or lost"
    for _ in range(8):
        await RisingEdge(dut.rclk)
    assert int(dut.r_valid.value) == 0, "valid after the last word"
    wclk.cancel()
    rclk.cancel()


def test_async_fifo_depth8():
    run("sl_async_fifo", ["sl_sync2.sv", "sl_async_fifo.sv"], "test_async_fifo",
        parameters={"W": 41, "AW": 3}, defines={}, name="async_fifo_aw3",
        env={"TB_AW": "3", "TB_SEED": "1"})


def test_async_fifo_depth4():
    run("sl_async_fifo", ["sl_sync2.sv", "sl_async_fifo.sv"], "test_async_fifo",
        parameters={"W": 41, "AW": 2}, defines={"SL_CDC_JITTER": 1}, name="async_fifo_aw2",
        env={"TB_AW": "2", "TB_SEED": "2"})
