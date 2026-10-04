"""Build-and-run helper shared by the cocotb unit tests.

Each test file holds the cocotb coroutines (run inside the simulator) and
plain pytest functions that call `run(...)` to compile the RTL with Icarus
Verilog and execute them.  Paths are absolute Path objects passed as
separate argv entries, so a checkout path with spaces or non-ASCII
characters is fine."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cocotb_tools.runner import get_runner

REPO = Path(__file__).resolve().parents[2]
RTL = REPO / "rtl"
MODEL = REPO / "model"


def run(toplevel: str, sources: list[str], test_module: str, parameters: dict | None = None,
        name: str | None = None, testcase: str | None = None, defines: dict | None = None,
        env: dict | None = None) -> None:
    sim = os.environ.get("SIM", "icarus")
    build_dir = REPO / "build" / "cocotb" / (name or toplevel)
    runner = get_runner(sim)
    runner.build(
        sources=[RTL / s for s in sources],
        hdl_toplevel=toplevel,
        build_dir=build_dir,
        includes=[RTL],
        parameters=parameters or {},
        defines=defines or {},
        always=True,
        timescale=("1ns", "1ps"),
        log_file=build_dir / "build.log",
    )
    extra = {"PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), str(MODEL)] + sys.path)}
    extra.update(env or {})
    runner.test(
        hdl_toplevel=toplevel,
        test_module=test_module,
        build_dir=build_dir,
        test_dir=REPO,
        results_xml=str(build_dir / "results.xml"),
        testcase=testcase,
        parameters=parameters or {},
        extra_env=extra,
    )
