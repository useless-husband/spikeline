"""Shared set-up for the single-clock PE tests."""

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from streams import reset

NCH = int(os.environ.get("TB_NCH", "8"))
SEED = int(os.environ.get("TB_SEED", "1"))


async def start(dut, cfg=0):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.cfg.value = cfg
    dut.clr.value = 0
    dut.in_valid.value = 0
    dut.out_ready.value = 0
    await reset(dut, dut.clk, dut.rst)
    for _ in range(NCH + 2):          # state-clearing sweep after reset
        await RisingEdge(dut.clk)
