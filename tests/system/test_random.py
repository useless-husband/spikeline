"""Randomised whole-chip runs: random routing (from the meaningful ones),
random PE settings, random clock periods and phases, clock jitter, random
input gaps and output backpressure, on all three builds (every PE in its
own clock domain, all synchronous, mixed).  Each seed is printed on failure
and can be replayed with SPIKELINE_SEED=<n>."""

import os
import random

import numpy as np
import pytest

import chip as C
import graph
import slmodel as M
import synthdata

NCH = 96
SEEDS = [int(s) for s in os.environ.get("SPIKELINE_SEED", "").split(",") if s] or list(range(1, 25))


def random_case(rng):
    cfgs = {
        "noise": M.cfg_noise(rng.randrange(1, 256), rng.randrange(0, 200)),
        "neo": M.cfg_neo(rng.randrange(300, 6000), rng.randrange(0, 60), rng.randrange(2)),
        "pred": M.cfg_predict(rng.randrange(4), rng.randrange(0, 9)),
    }
    routes = {}
    if rng.random() < 0.8:                     # spike chain
        noise_src = rng.choice(["bp", "bp", "in"])
        if noise_src == "bp":
            routes["bp"] = "in"
        routes["noise"] = noise_src
        routes["neo"] = "noise"
        if rng.random() < 0.8:
            routes["snip"] = "neo"
    if rng.random() < 0.7 or not routes:       # compression chain
        routes["pred"] = rng.choice([s for s in ["in", "bp", "noise", "neo"] if s == "in" or s in routes])
        routes["rice"] = "pred"
    active = [p for p in ("bp", "noise", "neo", "snip", "pred", "rice") if p in routes]
    feeders = {routes[p] for p in active}
    sinks = [p for p in active if p not in feeders]            # every PE output must go somewhere
    outs = ["out0", "out1", "out2"]
    rng.shuffle(outs)
    assert len(sinks) <= 3
    for o, p in zip(outs, sinks):
        routes[o] = p
    for o in outs[len(sinks):]:
        if rng.random() < 0.7:
            routes[o] = rng.choice(["in"] + [p for p in active if p not in ("snip", "rice")] or ["in"])
    periods = {"fab": rng.randrange(8000, 40000)}
    for p in ("bp", "noise", "neo", "snip", "pred", "rice"):
        periods[p] = rng.randrange(2000, 60000)
    return cfgs, routes, periods


@pytest.mark.parametrize("seed", SEEDS)
def test_random_configuration(seed):
    rng = random.Random(seed)
    variant = ["gals", "sync", "mixed"][seed % 3]
    cfgs, routes, periods = random_case(rng)
    frames = rng.randrange(200, 700)
    x = synthdata.make_recording(seconds=frames / 30000, snr=rng.uniform(3, 9), seed=100 + seed)["x"]
    x = x.astype(np.int64)
    s = C.Script().drain()
    for pe, v in cfgs.items():
        s.pe_cfg(pe, v, clear=True)
    s.routes(list(routes.items())).feed(frames).drain()
    ready = tuple(round(rng.uniform(0.3, 1.0), 2) for _ in range(3))
    res = C.run(f"random_{seed}", s, x, variant=variant, periods=periods, seed=seed, ready=ready,
                in_prob=rng.uniform(0.3, 1.0), jitter=rng.choice([0, 0, 100, 400]))
    want = graph.Golden(NCH, cfgs).run({k: v for k, v in routes.items() if not k.startswith("out")}, x)
    why = f"seed {seed} ({variant}) routes {routes} cfgs {cfgs} periods {periods}"
    for k in range(3):
        src = routes.get(f"out{k}")
        if src is None:
            assert len(res.port(k)) == 0, why
        else:
            C.compare(res.port(k), want[src], f"out{k} <- {src}; {why}")
