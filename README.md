# spikeline

A reconfigurable neural-signal processing chip for an implanted
brain-computer interface, designed in SystemVerilog and verified in
simulation.  It takes 96 electrodes x 30 kHz x 16 bit (46.08 Mbit/s), finds
spikes and cuts out their waveforms, compresses the raw stream losslessly, or
does both at once, with each processing element running in its own clock
domain at the lowest frequency its job needs.

**This is a learning reimplementation of published ideas, not new research.**
The architecture follows Yale's HALO research processor (Karageorgos, Sriram,
Bhattacharjee, Manohar et al.): a cluster of single-purpose accelerators,
each in its own clock domain, joined by a configurable interconnect so that
software can build different pipelines.  This project is not affiliated with
Yale or the HALO authors, uses none of their code, and differs from HALO in
the algorithms it implements (see [Related work](#related-work)).

[繁體中文說明](README.zh-TW.md) · [Course-style report](docs/report.md) ·
[Design notes](docs/DESIGN.md) · [導讀（給初學者）](docs/導讀.zh-TW.md)

![Three of 96 channels: raw input, band-passed signal with detected and true spikes, NEO energy against the adaptive threshold, and the aligned snippets, all taken from the simulated chip's output](docs/media/demo.png)

*Rendered by `make demo` from the words the simulated chip emitted (only the
raw input column is not chip output).  Synthetic recording, SNR 5.*

## What is inside

![Block diagram](docs/block_diagram.svg)

| Processing element | What it does | Cycles per sample |
|---|---|---|
| band-pass (`rtl/pe_bandpass.sv`) | 300-6000 Hz, 4th-order Butterworth as two direct-form-I biquads, one 24 x 18 multiplier time-shared | 13 |
| noise estimator (`rtl/pe_noise.sv`) | per-channel running median of \|x\| (the MAD noise estimate without sorting) | 1 |
| NEO detector (`rtl/pe_neo.sv`) | psi = x[n]^2 - x[n-1]x[n+1] > C x noise^2, refractory period; amplitude mode for comparison | 1 |
| snippet (`rtl/pe_snippet.sv`) | 32-sample window aligned to the spike's peak, from a per-channel ring buffer | 1 + 33 per spike |
| predictor (`rtl/pe_predict.sv`) | order 0/1/2 linear prediction, restarted every block so blocks decode independently | 1 |
| Rice coder (`rtl/pe_rice.sv`) | adaptive Rice code (LOCO-I style k), escape codes, MSB-first 32-bit words | 1 (2 for an escape) |

Around them: a crossbar with one route register per destination and
multicast (`rtl/sl_xbar.sv`); tiles that put a PE in its own clock domain
behind two asynchronous FIFOs with Gray-code pointers and two-flop
synchronisers (`rtl/sl_tile.sv`, `rtl/sl_async_fifo.sv`); a toggle handshake
for configuration across domains (`rtl/sl_cfg_cdc.sv`); a register block
with routes, PE settings, a "drained" status bit and counters
(`rtl/spikeline_top.sv`).  Every PE has a bit-exact numpy model in
`model/slmodel.py`.

Pipelines are register writes.  For example "spike detection and
compression at once" (pipeline c):

```
ROUTE[band-pass] = in      ROUTE[predictor] = in      (the input is multicast)
ROUTE[noise]     = band-pass
ROUTE[NEO]       = noise   ROUTE[Rice]      = predictor
ROUTE[snippet]   = NEO     ROUTE[out1]      = Rice
ROUTE[out0]      = snippet
```

## Results

All numbers were measured on this machine (Apple M5, shared with other
jobs) with the commands shown.  Simulation only: nothing here has run on an
FPGA or silicon.

**Verification** (`make test`: lint, 18 golden-model tests, 11 cocotb unit
suites, 29 whole-chip Verilator tests)

* Every PE matches the golden model word for word under random valid/ready
  gaps (cocotb + Icarus).
* The async FIFO passes order/loss/duplication checks at 7 clock ratios from
  1:30 to 30:1, random phase, with simulated metastability (a changing
  synchroniser bit randomly arrives a cycle late).
* The whole chip (Verilator, seven independent clocks) matches the model on
  all three pipelines, across run-time reconfiguration
  (spike -> drain -> compression -> drain -> both), and on 24 randomised
  configurations (routing, PE settings, clock periods and phases, jitter,
  backpressure; GALS, synchronous and mixed builds).
* `make metrics` runs the chip on 12 two-second recordings (6 SNRs x 2
  detector modes): 1.58-1.74 million output words per run, all identical
  to the model, and the compressed stream decodes to the exact input.
* `verilator --lint-only -Wall` is clean for all three builds.
* `make mutants`: 19 one-line bugs in the RTL and decoder, all caught
  ([docs/mutants.md](docs/mutants.md)).

**Spike detection** (`make metrics`; thresholds calibrated on a separate
recording; 3 seeds x 2 s x 96 channels per SNR; match within 0.5 ms;
[full table](docs/results/detection.md))

| SNR | NEO precision | NEO recall | NEO F1 | amplitude F1 |
|---:|---:|---:|---:|---:|
| 3 | 0.503 | 0.222 | 0.308 | 0.493 |
| 4 | 0.704 | 0.498 | 0.583 | 0.833 |
| 5 | 0.794 | 0.778 | 0.786 | 0.949 |
| 6 | 0.826 | 0.930 | 0.875 | 0.962 |
| 8 | 0.835 | 0.984 | 0.904 | 0.947 |

On these synthetic recordings the plain amplitude threshold (a mode of the
same PE) beats the energy operator; [DESIGN.md](docs/DESIGN.md) explains
why that depends on the background noise model.

**Compression** (`make metrics`; [details](docs/results/compression.md)):
order-1 prediction + adaptive Rice gives **1.853x** at SNR 5 (8.63 bits per 16-bit sample, against a zeroth-order residual entropy of 8.42 bits), almost independent of SNR; order 0 gives 1.313x and order 2 1.729x.  Blocks of 128 frames (4.3 ms) are independently decodable; 8-frame blocks drop the ratio to 1.562x, 2048-frame blocks raise it to 1.876x.  Every stream was decoded back to the exact input.

**Throughput and minimum clocks** (`make bench`;
[details](docs/results/throughput.md)).  Real time is 2.88 M samples/s.

| PE | measured cycles/sample | worst case | minimum clock (worst case) | real-time check |
|---|---:|---:|---:|---|
| band-pass | 13.000 | 13.000 | 37.44 MHz | 0 overruns at 38.563 MHz, 287192 at 36.317 MHz |
| noise | 1.000 | 1.000 | 2.88 MHz | 0 overruns at 2.966 MHz, 287331 at 2.794 MHz |
| NEO | 1.000 | 1.000 | 2.88 MHz | 0 overruns at 2.966 MHz, 286836 at 2.794 MHz |
| snippet | 1.056 | 2.500 | 7.20 MHz | lowest clock without overruns on this data: 4.379 MHz |
| predictor | 1.000 | 1.000 | 2.88 MHz | 0 overruns at 2.966 MHz, 287317 at 2.794 MHz |
| Rice | 1.000 | 2.000 | 5.76 MHz | lowest clock without overruns on this data: 2.905 MHz |

**Latency** (real-time input, clocks fab 10 MHz, bp 40 MHz, noise 5 MHz, neo 5 MHz, snip 8 MHz, pred 3.2 MHz, rice 3.6 MHz): a NEO detection leaves the chip 37.4 us (median; 37.0-41.0 us) after its sample was digitised, of which 33.3 us is the wait for the next sample that the energy operator needs; a snippet packet leaves 738.4 us after its peak sample, of which 733.3 us is the wait for the 21 samples after the peak.

**Resources** (`make synth`: Yosys `synth_xilinx`, Spartan-7 XC7S50, no
place and route; [report](synth/report.md))

| Block | LUT | LUTRAM | FF | BRAM36 | DSP48E1 | CARRY4 |
|---|---:|---:|---:|---:|---:|---:|
| band-pass filter (2 biquads, 1 multiplier) | 430 | 0 | 138 | 2 | 1 | 33 |
| noise estimator (median tracker) | 193 | 12 | 66 | 0 | 0 | 28 |
| NEO / amplitude detector | 216 | 21 | 35 | 0 | 6 | 22 |
| snippet extractor (incl. ring buffer) | 246 | 15 | 100 | 2 | 0 | 36 |
| linear predictor | 206 | 18 | 54 | 0 | 0 | 19 |
| adaptive Rice coder + bit packer | 894 | 12 | 152 | 0 | 0 | 34 |
| **six PEs** | 2,185 | 78 | 545 | 4 | 7 | 172 |
| **whole chip** (PEs + async FIFOs + crossbar + registers) | 4,307 | 190 | 1,832 | 4 | 7 | 252 |

**Switching activity** is reported in
[docs/results/activity.md](docs/results/activity.md) as transitions per
sample from Verilator toggle coverage.  It is a relative proxy only; this
project reports no power numbers, because no open-source sign-off power
flow exists for it.

## Compared with HALO (published numbers vs. this project)

| | HALO, as published (IEEE Micro 2023) | spikeline, measured here |
|---|---|---|
| input | 96 channels x 30 kHz x 16 bit = 46 Mbps | same (46.08 Mbit/s) |
| implementation | taped out in 12 nm (some PEs), 28 nm synthesis, Cadence tools | RTL, simulated; FPGA synthesis numbers only |
| spike pipeline | NEO -> THR -> GATE | band-pass -> noise -> NEO -> snippet |
| compression | LZ4, LZMA (LZ -> MA -> RC), DWT | linear prediction + adaptive Rice |
| NEO PE clock | 3 MHz (28 nm), 14 MHz (12 nm) | 1 cycle/sample: 2.88 MHz minimum, checked at 2.97 MHz |
| per-PE clocks | 3-180 MHz | 2.88-37.44 MHz worst case |
| power | at most 6.3 mW per pipeline (12 nm) | not measured (no sign-off tool) |

The HALO numbers are copied from the paper and were not reproduced.  The
similar NEO clock is expected rather than meaningful: both process one
sample per cycle.

## Build and test

Needs Verilator (5.020 or newer), Icarus Verilog (12+), Yosys (0.33+),
Python 3.11+, a C++17 compiler.  Every command runs from the repository
root; all paths in the Makefile are relative, so the checkout may live in a
directory with spaces or non-ASCII characters.

```sh
make venv          # once: .venv with numpy, scipy, matplotlib, cocotb, pytest
make test          # lint + golden model + cocotb unit tests + whole-chip tests (a few minutes)
make demo          # build/demo/demo.png
make metrics       # detection and compression tables, with the hardware check (about 7 min)
make bench         # cycles per sample, real-time check, latency (about 10 min)
make quant         # filter fixed-point sweep
make synth         # Yosys resource report
make mutants       # mutation check (about 3 min)
make activity      # switching-activity proxy
```

On macOS, double-click `跑跑看.command` to build the simulator, run the
demo and open the picture (it checks for the tools and explains what is
missing).  CI (`.github/workflows/ci.yml`) uses Ubuntu's packaged Verilator
5.020, Icarus 12 and Yosys 0.33; Yosys 0.33 was also run locally (through
the `yowasp-yosys` package) and synthesises the design, with more LUTs and
DSPs than 0.69 reports.  Verilator 5.020 and Icarus 12 were not available
locally.

## Limitations

* Simulation only.  No timing closure, no placement, no board run; Yosys
  maps to 7-series cells but gives no timing.
* No power figures.  The activity counts are a relative proxy.
* Synthetic data only.  The generator's noise model (white, 1/f, many
  small distant spikes) decides how well each detector does; a public
  recording with a clear licence was not added.
* Simulation cannot show metastability.  The CDC design follows the
  standard rules and the tests inject late arrivals, but that demonstrates
  the protocol, not the absence of every CDC hazard (DESIGN.md explains a
  case the tests cannot tell apart).
* Routes and PE settings may only change while the chip is drained (STATUS
  bit 16); the hardware does not enforce it.
* The chip's input has a 16-word FIFO; bursty PEs (snippet, Rice) need
  clock headroom along the whole path to absorb bursts in real time
  (measured in [throughput.md](docs/results/throughput.md)).
* The crossbar trusts that tags are valid channel numbers (< 96).
* No encryption, radio, ADC or microcontroller, which HALO has.

## Related work

* **HALO** - I. Karageorgos, K. Sriram, J. Vesely, M. Wu, M. Powell, D.
  Borton, R. Manohar, A. Bhattacharjee, "Hardware-software co-design for
  brain-computer interfaces", ISCA 2020; "Balancing specialized versus
  flexible computation in brain-computer interfaces", IEEE Micro 41(3),
  2021; "HALO: A Hardware-Software Co-Designed Processor for Brain-Computer
  Interfaces", IEEE Micro, 2023.  The source of the architecture idea: PEs
  in separate clock domains, a configurable interconnect, pipelines built by
  software, 96 x 30 kHz x 16 bit.  HALO's PEs (NEO, THR, GATE, LZ, MA, RC,
  DWT, FFT, XCOR, BBF, SVM, AES) are taped out; this project reimplements
  the structure with a different set of PEs and only in simulation.
* **SCALO** - K. Sriram et al., "SCALO: An Accelerator-Rich Distributed
  System for Scalable Brain-Computer Interfacing", ISCA 2023: several
  HALO-like implants networked wirelessly, with on-device storage.  Not
  attempted here.
* R. Pothukuchi and A. Bhattacharjee, "The Brain-Computer Interfacing
  Landscape for Computer Architects", SIGARCH blog, January 2024: the
  constraints (1 degree C heating, milliwatt budgets, radio bandwidth) that
  motivate on-implant processing.
* NEO for spike detection: J. F. Kaiser, "On a simple algorithm to calculate
  the 'energy' of a signal", ICASSP 1990; S. Mukhopadhyay and G. C. Ray, "A
  new interpretation of nonlinear energy operator and its efficacy in spike
  detection", IEEE TBME, 1998.
* Median-based threshold: R. Q. Quiroga, Z. Nadasdy, Y. Ben-Shaul,
  "Unsupervised spike detection and sorting with wavelets and
  superparamagnetic clustering", Neural Computation, 2004.
* Hardware spike detection with source code: an FPGA spike detector using a
  smoothed NEO and a Butterworth high-pass filter, "FPGA Design Integration
  of a 32-Microelectrodes Low-Latency Spike Detector in a Commercial System
  for Intracortical Recordings", Digital (MDPI), 2021.
* On-chip neural data compression: J. N. Y. Aziz et al., "256-Channel Neural
  Recording and Delta Compression Microsystem With 3D Electrodes", IEEE JSSC,
  2009 (cited by HALO).
* Adaptive Golomb-Rice parameter: M. J. Weinberger, G. Seroussi, G. Sapiro,
  "The LOCO-I lossless image compression algorithm", IEEE TIP, 2000.
* Asynchronous FIFO design: C. E. Cummings, "Simulation and Synthesis
  Techniques for Asynchronous FIFO Design", SNUG 2002.

To my knowledge there is no open-source RTL of HALO; this project does not
claim to match its design beyond the ideas listed above.

## Repository layout

```
rtl/            SystemVerilog: six PEs, async FIFO, synchronisers, tile, crossbar, top
model/          bit-exact golden model, independent decoder, synthetic recording generator
sim/            Verilator harness: seven clocks, ADC pacing, backpressure, register script
tests/model     golden-model tests          tests/unit    cocotb unit tests (Icarus)
tests/system    whole-chip tests (Verilator)
tools/          coefficients, metrics, bench, quantisation sweep, synthesis report, mutants, demo
synth/          synthesis report            docs/         report, design notes, results, 導讀
```

## License

MIT, see [LICENSE](LICENSE).
