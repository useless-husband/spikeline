"""Drive the Verilator model of the whole chip (build/vsim_*) from Python.

A run writes the input samples and a register script to build/sysrun/<name>/,
starts the simulator and returns every word that left the three output
ports, with its port and time, plus the simulator's log lines."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build"

SRC = {"in": 0, "bp": 1, "noise": 2, "neo": 3, "snip": 4, "pred": 5, "rice": 6, None: 15}
DST = {"bp": 0, "noise": 1, "neo": 2, "snip": 3, "pred": 4, "rice": 5, "out0": 6, "out1": 7, "out2": 8}
PE = {"bp": 0, "noise": 1, "neo": 2, "snip": 3, "pred": 4, "rice": 5}
CLOCKS = ("fab", "bp", "noise", "neo", "snip", "pred", "rice")
ID, PARAMS, STATUS = 0x00, 0x01, 0x02
IN_WORDS, OUT_WORDS = 0x30, 0x31

# the three pipelines of the report; (destination, source)
SPIKE = [("bp", "in"), ("noise", "bp"), ("neo", "noise"), ("snip", "neo"), ("out0", "snip")]
COMPRESS = [("pred", "in"), ("rice", "pred"), ("out1", "rice")]
BOTH = SPIKE + COMPRESS

REC = np.dtype([("data", "<u4"), ("tag", "u1"), ("last", "u1"), ("port", "u1"), ("pad", "u1"),
                ("time", "<u8")])


class Script:
    def __init__(self):
        self.lines: list[str] = []

    def w(self, addr, value):
        self.lines.append(f"w {addr:#x} {value & 0xFFFFFFFF:#x}")
        return self

    def r(self, addr):
        self.lines.append(f"r {addr:#x}")
        return self

    def expect(self, addr, value):
        self.lines.append(f"expect {addr:#x} {value & 0xFFFFFFFF:#x}")
        return self

    def route(self, dst, src):
        return self.w(0x10 + DST[dst], SRC[src])

    def routes(self, pairs, clear_all=True):
        if clear_all:
            for d in DST:
                self.route(d, None)
        for d, s in pairs:
            self.route(d, s)
        return self

    def pe_cfg(self, pe, value, clear=False):
        return self.w(0x20 + PE[pe], value | (1 << 31 if clear else 0))

    def feed(self, frames):
        self.lines.append(f"feed {frames}")
        return self

    def drain(self):
        self.lines.append("drain")
        return self

    def wait(self, cycles):
        self.lines.append(f"wait {cycles}")
        return self

    def mark(self, name):
        self.lines.append(f"mark {name}")
        return self

    def text(self):
        return "\n".join(self.lines) + "\n"


@dataclass
class Result:
    records: np.ndarray
    log: list[str]
    stats: dict = field(default_factory=dict)
    marks: dict = field(default_factory=dict)
    feeds: list = field(default_factory=list)
    reads: dict = field(default_factory=dict)

    def port(self, k, start=None, end=None):
        """(data, tag, last) rows of output port k, optionally between two marks."""
        r = self.records
        idx = np.arange(len(r))
        sel = r["port"] == k
        if start is not None:
            sel &= idx >= self.marks[start]
        if end is not None:
            sel &= idx < self.marks[end]
        r = r[sel]
        return np.stack([r["data"].astype(np.int64), r["tag"].astype(np.int64),
                         r["last"].astype(np.int64)], 1)

    def times(self, k):
        return self.records["time"][self.records["port"] == k]


def run(name, script: Script, x=None, variant="gals", periods=None, seed=1, ready=(1.0, 1.0, 1.0),
        in_prob=1.0, adc_period=0, jitter=0, max_time=10**13, nch=96, coverage=None) -> Result:
    d = BUILD / "sysrun" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "script.txt").write_text(script.text())
    cmd = [str(BUILD / f"vsim_{variant}"), "--script", str(d / "script.txt"), "--out", str(d / "out.bin"),
           "--nch", str(nch), "--seed", str(seed), "--ready", ",".join(str(p) for p in ready),
           "--in-prob", str(in_prob), "--adc-period", str(adc_period), "--jitter", str(jitter),
           "--max-time", str(max_time)]
    if x is not None:
        np.asarray(x, dtype="<i2").tofile(d / "in.bin")
        cmd += ["--in", str(d / "in.bin")]
    for k, v in (periods or {}).items():
        cmd += ["--period", f"{k}={int(v)}"]
    if coverage:
        cmd += ["--coverage", str(coverage)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    log = p.stdout.splitlines()
    (d / "log.txt").write_text(p.stdout + p.stderr)
    assert p.returncode == 0, f"simulator failed ({p.returncode}): {p.stderr.strip()[-2000:]}"
    res = Result(np.fromfile(d / "out.bin", dtype=REC), log)
    for line in log:
        f = line.split()
        if f[0] == "mark":
            res.marks[f[1]] = int(f[3])
        elif f[0] == "feed":
            res.feeds.append((int(f[1]), int(f[2])))
        elif f[0] == "r":
            res.reads.setdefault(int(f[1], 16), []).append(int(f[2], 16))
        elif f[0] == "done":
            res.stats = {f[i]: int(f[i + 1]) for i in range(1, len(f), 2)}
    return res


def compare(got, want, what):
    want = np.asarray(want, dtype=np.int64).reshape(-1, 3)
    assert len(got) == len(want), f"{what}: {len(got)} words out, golden model says {len(want)}"
    bad = np.nonzero(np.any(got != want, axis=1))[0]
    assert len(bad) == 0, (f"{what}: {len(bad)} words differ, first at {bad[0]}: "
                           f"got {got[bad[0]].tolist()} want {want[bad[0]].tolist()}")
