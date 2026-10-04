# spikeline: a GALS neural-signal processing fabric for an implanted brain-computer interface

*Course-style final report.  A learning reimplementation of ideas published
for Yale's HALO processor; not affiliated with Yale, not new research.
Everything below was measured in simulation on this repository with the
commands given; nothing has run on an FPGA or silicon.*

## Contents

1. Goals and scope
2. System specification
3. Block diagram
4. Processing elements
5. Interconnect and reconfiguration
6. GALS and clock-domain crossing
7. Fixed-point analysis
8. Verification strategy
9. Results
10. Comparison with HALO
11. Challenges and bugs found
12. Lessons learned
13. Future work
14. Reproducing every number

## 1. Goals and scope

An implanted brain-computer interface must process tens of Mbit/s of
neural data in real time inside a power budget of roughly 15 mW for the
whole device, because the implant may not heat brain tissue by more than
1 degree C (HALO, IEEE Micro 2023).  A radio at that power carries only a few
Mbit/s, so the data must be reduced on the implant: either by detecting
spikes and sending short waveforms, or by lossless compression.

HALO's answer is a set of small accelerators (processing elements, PEs),
each in its own clock domain at the lowest frequency its job needs,
connected by a configurable interconnect so that one chip serves several
applications.  This project rebuilds that *structure* from scratch:

* six PEs: band-pass filter, noise estimator, NEO spike detector, snippet
  extractor, linear predictor, adaptive Rice coder;
* a crossbar whose routing table is written over a register interface, with
  multicast, so pipelines can be built and changed at run time;
* GALS tiles: every PE can run on its own clock behind asynchronous FIFOs
  written for this project;
* a bit-exact golden model, a synthetic recording generator with ground
  truth, and measurements of detection quality, compression, throughput,
  latency and FPGA resources.

Out of scope: radio, ADC, encryption, a microcontroller, timing closure,
power sign-off, real recordings.

## 2. System specification

**Input.** 96 channels x 30 kHz x 16-bit signed samples = 2.88 M
samples/s = 46.08 Mbit/s, as one time-multiplexed stream: frame *f*
carries channels 0..95 in order.  The synthetic recordings use 0.195 uV per
LSB, the step of common neural amplifier ADCs.

**Stream words.** Every crossbar port carries `{last, tag[7:0], data[31:0]}`
with valid/ready handshaking; a word moves on a clock edge where both are
high.  `tag` is the channel (or the block number for compressed words),
`last` marks the final channel of a frame (or the final word of a packet).

| producer | data[31:0] | words per input sample |
|---|---|---|
| input port, band-pass | sign-extended sample | 1 |
| noise estimator | `{ready, noise[14:0] (Q11.4), x[15:0]}` | 1 |
| NEO detector | `{15'b0, det, x[15:0]}` (one frame late) | 1 |
| snippet | header = peak frame number, then 32 sign-extended samples | 33 per spike |
| predictor | `{lastblk, first, 10'b0, e[19:0]}` | 1 |
| Rice coder | packed bitstream, MSB first | about 0.27 |

**Register map** (32-bit words, fabric clock domain):

| address | name | meaning |
|---|---|---|
| 0x00 | ID | 0x53504B4C ("SPKL") |
| 0x01 | PARAMS | NCH, sources (7), destinations (9), ASYNC_MASK |
| 0x02 | STATUS | tile idle [5:0], in port empty [6], out ports empty [9:7], config busy [15:10], drained [16] |
| 0x10-0x18 | ROUTE[d] | source for destination d; 15 = none |
| 0x20-0x25 | PE_CFG[p] | PE settings; bit 31 = clear the PE's state |
| 0x30-0x33 | counters | words in, words out per port |

Crossbar sources: 0 input port, 1 band-pass, 2 noise, 3 NEO, 4 snippet,
5 predictor, 6 Rice.  Destinations: 0-5 the PEs in the same order, 6-8
output ports.  PE settings: noise `[7:0]` step, `[23:8]` warm-up; NEO
`[15:0]` C, `[23:16]` refractory frames, `[24]` amplitude mode; predictor
`[1:0]` order, `[5:2]` block size log2.

## 3. Block diagram

![Block diagram](block_diagram.svg)

Each dashed box is a separate clock domain when `ASYNC_MASK` has its bit set
(the default build sets all six).  The crossbar, registers and ports are in
the fabric clock domain.

## 4. Processing elements

All PEs share one port list (`clk, rst, cfg, clr, in_*, out_*, idle`), keep
per-channel state in a memory indexed by `tag`, never let `in_ready` depend
on `in_valid`, register `out_valid`, and run a clear sweep (NCH cycles) after
reset or a clear command.  The bit-exact reference of each is the class of
the same name in `model/slmodel.py`.

### 4.1 Band-pass filter (`pe_bandpass.sv`)

* Function: 4th-order Butterworth band-pass 300-6000 Hz as two
  direct-form-I biquads, high-pass section first (removes the electrode
  offset and field potential before the second section).
* Datapath: one 24 x 18 multiplier and a 48-bit accumulator, stepped through
  11 multiply-accumulate steps by a counter; per channel six stored values
  (128 bits) in a 96-entry memory.  Yosys maps the memory to block RAM
  because its read data is only used registered.
* Timing: 13 cycles per sample, every sample, so 13 x 2.88 MHz = 37.44 MHz.
* Fixed point: section 7.

### 4.2 Noise estimator (`pe_noise.sv`)

* Function: per channel, m <- m + step if |x| > m, m - step if |x| < m.
  m settles at the median of |x|; sigma = median / 0.6745 for Gaussian
  noise (the factor is folded into the detector's constant).  A frame
  counter raises `ready` after `warmup` frames.
* Settings: step (1/256 LSB, default 16), warm-up (default 1024 frames).
* Timing: 1 cycle per sample.
* Check: on Gaussian noise with sigma 5, 40 and 300 LSB the estimate is within
  6 % (`tests/model/test_model.py::test_noise_tracker_finds_the_median`).

### 4.3 NEO detector (`pe_neo.sv`)

* Function: psi[n] = x[n]^2 - x[n-1] x[n+1] in 33 bits; detection if
  `ready`, psi > (noise^2 x C) >> 16 and the channel is not refractory;
  after a detection the channel ignores R frames.  Amplitude mode:
  |x[n]| > (noise x C) >> 12.
* Because psi[n] needs x[n+1], the PE outputs x[n] when x[n+1] arrives: the
  stream leaves one frame late, first frame zero.
* Settings: C (16 bits), R (8 bits, default 30 = 1 ms), mode bit.
  Defaults C = 2700 (energy) and 1500 (amplitude) were calibrated (9.2).
* Timing: 1 cycle per sample; 6 DSP48E1 (two 16 x 16 products for psi and
  the threshold products).

### 4.4 Snippet extractor (`pe_snippet.sv`)

* Function: ring buffer of the last 32 samples per channel (block RAM,
  address `{channel, frame mod 32}`).  A detection starts a 10-sample peak
  search (largest |x|, first on ties); once the 21st sample after the peak
  has arrived, the packet is sent: a header with the peak's frame number and
  the 32 samples [peak - 10, peak + 21].
* The read port is driven with the *next* address, so the data for the
  current address is always ready and the packet streams at one word per
  cycle; input is stalled for the 33 cycles of a packet.
* Timing: 1 cycle per sample plus 33 per packet.  A channel can start a new
  packet at most every 22 frames, so the worst case is 1 + 33/22 = 2.5
  cycles per sample (7.2 MHz).

### 4.5 Linear predictor (`pe_predict.sv`)

* Function: e = x, x - x[n-1] or x - 2x[n-1] + x[n-2].  Blocks of 2^B
  frames (default 128); frame fb of a block uses order min(order, fb), so a
  block never depends on the previous one.  Flags mark the first frame and
  the last sample of each block.
* Timing: 1 cycle per sample.

### 4.6 Adaptive Rice coder (`pe_rice.sv`)

* Function: u = zigzag(e); per channel A <- A - A/16 + u (16 x running mean,
  reset to 256 at each block); k = min(floor(log2(A/16)), 15); code = q = u >>
  k ones, a zero, k low bits; q >= 16 is escaped as 16 ones and 20 raw bits.
  The first frame of a block (raw samples from the restarted predictor) is
  sent as plain 20-bit values.  A 64-bit buffer packs codes MSB first into
  32-bit words; after a block's last sample it pads to a word and marks the
  word `last`.  `tag` = block number.
* Timing: 1 cycle per sample, 2 for an escape, plus at most one flush cycle
  per block; worst case 2 cycles per sample (5.76 MHz).
* Decoder: `model/sl_decode.py`, written independently of the encoder model.

## 5. Interconnect and reconfiguration

`sl_xbar.sv`: for each of the 9 destinations an 8:1 multiplexer chooses
the source named by its route register.  Several destinations may name the
same source.  Multicast uses an eager fork: destination d sets `done[d]`
when it takes the current word; the source's ready is the AND, over the
destinations listening to it, of `done | ready`; when the source's word
moves, the `done` bits of its listeners clear.  Every listener gets every
word exactly once, and a slow listener only delays its siblings.  A source
nobody listens to is stalled, never dropped.

Because tile inputs are FIFOs with registered ready and PE outputs are
registered, no combinational path crosses a tile, so any routing - even a
loop - is free of combinational cycles.

**Reconfiguration protocol** (exercised by
`tests/system/test_pipelines.py::test_reconfiguration_at_run_time`): stop
the input; poll STATUS until `drained`; write the routes (a route write also
clears the fork state); optionally write PE settings or clear PEs; resume.
PE state survives a reconfiguration, which the golden model reproduces by
keeping its PE objects.  `drained` needs every tile idle for 7 consecutive
cycles, both FIFO views empty, no configuration in flight, and the ports
empty.

## 6. GALS and clock-domain crossing

Each tile with its `ASYNC_MASK` bit set has:

* **Two asynchronous FIFOs** (8 entries, `sl_async_fifo.sv`).  Each side
  keeps a binary pointer and its Gray code in registers; only the Gray
  pointers cross, each through a two-flop synchroniser (`sl_sync2.sv`).
  Gray code changes one bit per increment, so a synchroniser that samples
  during a change returns the old or the new value, never a value that is
  neither.  Full is computed on the write side as "next write Gray pointer
  equals the synchronised read pointer with its top two bits inverted",
  empty on the read side as "next read pointer equals the synchronised write
  pointer"; both are registered, so `w_ready` and `r_valid` are glitch-free
  flops.  A synchronised pointer is always a few cycles old, which only
  makes each side conservative.  Data is written in the write domain and
  read combinationally in the read domain; a slot is read only after its
  pointer update has crossed, at least two read-clock edges after the write.
* **A configuration crossing** (`sl_cfg_cdc.sv`): the fabric side loads a
  holding register and toggles `req`; the PE side sees the toggle through a
  synchroniser, copies the holding register (stable for at least two of its
  cycles and unchanged until acknowledged), applies it - for a clear, waits
  for its clear sweep to start and end - and toggles `ack`.  The register
  bus stalls writes while a crossing is busy.  Only single-bit toggles are
  synchronised.
* **A reset synchroniser** per domain: asynchronous assert, synchronous
  release; logic then uses a synchronous reset.
* **Idle reporting**: PE-side idle crosses through a synchroniser and is
  combined with the fabric's view of both FIFOs; because these views use
  different synchronisers and can arrive a cycle apart, idle must hold for
  7 cycles.

**Testing CDC in simulation.** Simulators have no metastability.  With
`SL_CDC_JITTER` defined, a synchroniser bit that has just changed is
captured one cycle late at random.  The async FIFO test runs 7 clock ratios
(1:1, 1:1.001, 1:3.7, 3.7:1, 7:23, 1:30, 30:1) x 4 valid/ready densities x 2
depths, a random phase each time, and checks order, loss, duplication and
that `w_ready` is never high on a full FIFO.  The configuration test runs 5
ratios with a fake PE that clears for several cycles.  The Verilator
harness runs all seven chip clocks with random phases and optional
per-edge jitter.

What this cannot show: with one transfer per cycle and registered flags, a
one-cycle inconsistent pointer can only expose a slot that has already been
written, so the same tests also pass with binary pointers crossing on both
sides (tried during development).  Gray coding is kept because it is
correct by construction for any use of the pointers.  The tests do catch a
Gray/binary mismatch and a wrong full formula (mutants `afifo_gray`,
`afifo_full`).

## 7. Fixed-point analysis

**Band-pass filter** (`make quant`,
[results/quantisation.md](results/quantisation.md)).  Coefficients are
Q2.16 (18 bits), states Q18.6 (24 bits: 4x integer headroom over the 16-bit
input), accumulator 48 bits (exact), one rounding per section.

| coefficient frac. bits CF | response error 300-6000 Hz (dB) | pole radius | FS=0 | FS=2 | FS=4 | FS=6 | FS=8 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 8 | 0.210 | 0.956066 | 10.473 | 3.030 | 1.559 | 1.437 | 1.428 |
| 10 | 0.380 | 0.957087 | 10.575 | 5.261 | 4.714 | 4.685 | 4.679 |
| 12 | 0.033 | 0.956959 | 10.195 | 2.604 | 0.777 | 0.447 | 0.421 |
| 14 | 0.007 | 0.956896 | 10.309 | 2.612 | 0.719 | 0.342 | 0.302 |
| 16 (chip) | 0.001 | 0.956896 | 10.292 | 2.603 | 0.710 | 0.331 | 0.292 |

(FS = fractional bits of the filter state.)  Effect on detection (SNR 5, 1 s, energy mode):

| filter | precision | recall | F1 |
|---|---:|---:|---:|
| double precision (rounded to 16-bit for the next PE) | 0.775 | 0.783 | 0.779 |
| chip: CF 16, FS 6 | 0.774 | 0.784 | 0.779 |
| CF 10, FS 0 | 0.793 | 0.761 | 0.777 |
| CF 8, FS 0 | 0.776 | 0.773 | 0.774 |

The high-pass poles sit at radius 0.957, so rounding inside its feedback
loop is amplified: state precision matters far more than coefficient
precision.  With 6 fractional state bits the error is 0.33 LSB rms, against
0.29 LSB for rounding the output to whole LSBs alone.  At 10 coefficient
bits the pass-band gain is off by 0.38 dB, which shows as a 4.7 LSB error on
this signal.  Spike detection barely notices any of this (last table).  The
high-pass numerator (1, -2, 1) is exactly representable, so the zero at DC
is exact.

**Other widths.** psi needs 33 signed bits (x^2 up to 2^30, x[n-1]x[n+1]
from -2^30 to 2^30).  The noise tracker is Q16.8 in 24 bits; |x| << 8 is at
most 2^23, so it cannot overflow.  The Rice accumulator obeys A <= 16 x
max(u) < 2^24.  Residuals of order 2 fit 18 bits; the coder accepts 20.

## 8. Verification strategy

| Level | What | Where | Count |
|---|---|---|---:|
| golden model | hand-computed bitstream, decoder round trips for every order and block size, each block decoded alone, filter vs. double precision, median tracker vs. true median, NEO vs. formula, generator reproducibility | `tests/model` | 18 tests |
| PE unit | each PE vs. the model with random valid/ready gaps, extreme inputs (full-scale steps, -32768, ties), 8 and 96 channels, clear; throughput assertions | `tests/unit` (cocotb, Icarus) | 8 suites |
| CDC | async FIFO, 7 ratios x 4 densities x 2 depths; configuration crossing, 5 ratios; jitter injection on | `tests/unit` | 3 suites |
| whole chip | register map; spike, compression and parallel pipelines; run-time reconfiguration; 24 randomised configurations; three builds | `tests/system` (Verilator) | 29 tests |
| long runs | 12 x 2 s recordings through the chip, every word compared, decoded | `make metrics` | 12 runs |
| lint | `verilator --lint-only -Wall`, three builds | `make lint` | clean |
| mutation | 19 one-line bugs, each must fail a suite | `make mutants` | 19/19 caught |
| real time | ADC-paced input, overruns counted | `make bench` | section 9.4 |

The randomised whole-chip test draws, from a fixed seed, a meaningful
routing (spike chain with or without filter; compression of the raw,
filtered, noise or NEO stream; random probes on spare outputs), PE
settings, seven clock periods (fabric 25-125 MHz, PEs 17-500 MHz), clock
jitter, input gaps and output backpressure, and one of three builds (every
PE in its own domain, all synchronous, alternate).  The golden evaluator
(`tests/system/graph.py`) computes every source's stream for any such
routing.  A failing seed is printed and can be replayed with
`SPIKELINE_SEED=<n>`.

## 9. Results

### 9.1 Bit-exactness

All 58 pytest tests pass (`make test`).  In the long runs of
`make metrics` the chip produced between 1.58 and 1.74 million output words per two-second
recording (snippets and compressed words); every word matched the golden
model, and every compressed stream decoded to the exact input.

### 9.2 Spike detection versus SNR

Method: SNR = peak of the band-passed spike / standard deviation of the
band-passed background on that channel.  Thresholds were chosen on a
calibration recording (seed 1000, SNR 5) by the best F1 and then frozen;
evaluation uses seeds 2001-2003, 2 s x 96 channels each.  A detection is a
snippet's aligned peak; it matches a ground-truth spike on the same channel
within +-15 samples (0.5 ms), one to one.  The first 2048 frames (warm-up)
are not scored.

**Calibration** (seed 1000, SNR 5, 1 s):

| Mode | C | Threshold | Precision | Recall | F1 |
|---|---:|---|---:|---:|---:|
| energy (NEO) | 1800 | psi > 7.0 x median(|x|)^2 | 0.317 | 0.957 | 0.476 |
| energy (NEO) | 2100 | psi > 8.2 x median(|x|)^2 | 0.495 | 0.929 | 0.646 |
| energy (NEO) | 2400 | psi > 9.4 x median(|x|)^2 | 0.667 | 0.868 | 0.754 |
| energy (NEO) | 2550 | psi > 10.0 x median(|x|)^2 | 0.741 | 0.833 | 0.785 |
| energy (NEO) | 2700 | psi > 10.5 x median(|x|)^2 | 0.799 | 0.795 | 0.797 |
| energy (NEO) | 2850 | psi > 11.1 x median(|x|)^2 | 0.840 | 0.742 | 0.788 |
| energy (NEO) | 3000 | psi > 11.7 x median(|x|)^2 | 0.882 | 0.690 | 0.774 |
| energy (NEO) | 3300 | psi > 12.9 x median(|x|)^2 | 0.935 | 0.580 | 0.716 |
| energy (NEO) | 3600 | psi > 14.1 x median(|x|)^2 | 0.961 | 0.480 | 0.641 |
| amplitude | 1200 | |x| > 4.69 x median(|x|) | 0.528 | 0.985 | 0.688 |
| amplitude | 1300 | |x| > 5.08 x median(|x|) | 0.727 | 0.981 | 0.835 |
| amplitude | 1400 | |x| > 5.47 x median(|x|) | 0.870 | 0.975 | 0.920 |
| amplitude | 1450 | |x| > 5.66 x median(|x|) | 0.913 | 0.970 | 0.940 |
| amplitude | 1500 | |x| > 5.86 x median(|x|) | 0.947 | 0.958 | 0.952 |
| amplitude | 1550 | |x| > 6.05 x median(|x|) | 0.966 | 0.938 | 0.952 |
| amplitude | 1600 | |x| > 6.25 x median(|x|) | 0.978 | 0.913 | 0.944 |
| amplitude | 1700 | |x| > 6.64 x median(|x|) | 0.992 | 0.851 | 0.916 |
| amplitude | 1800 | |x| > 7.03 x median(|x|) | 0.997 | 0.761 | 0.864 |

**Evaluation** (seeds 2001-2003):

| SNR | true spikes | energy (NEO): precision | recall | F1 | amplitude: precision | recall | F1 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 | 15629 | 0.231 | 0.071 | 0.109 | 0.558 | 0.077 | 0.135 |
| 3 | 15629 | 0.503 | 0.222 | 0.308 | 0.859 | 0.345 | 0.493 |
| 4 | 15629 | 0.704 | 0.498 | 0.583 | 0.932 | 0.754 | 0.833 |
| 5 | 15629 | 0.794 | 0.778 | 0.786 | 0.944 | 0.953 | 0.949 |
| 6 | 15629 | 0.826 | 0.930 | 0.875 | 0.939 | 0.986 | 0.962 |
| 8 | 15629 | 0.835 | 0.984 | 0.904 | 0.909 | 0.990 | 0.947 |

**The same runs through the chip** (seed 2001; every output word compared with the model):

| SNR | mode | snippet packets | words compared | identical | compressed words | decoded = input |
|---:|---|---:|---:|---|---:|---|
| 2 | energy | 1817 | 1610396 | yes | 1550435 | yes |
| 2 | amplitude | 755 | 1575350 | yes | 1550435 | yes |
| 3 | energy | 2392 | 1629695 | yes | 1550759 | yes |
| 3 | amplitude | 2005 | 1616924 | yes | 1550759 | yes |
| 4 | energy | 3572 | 1669012 | yes | 1551136 | yes |
| 4 | amplitude | 3847 | 1678087 | yes | 1551136 | yes |
| 5 | energy | 4847 | 1711481 | yes | 1551530 | yes |
| 5 | amplitude | 4805 | 1710095 | yes | 1551530 | yes |
| 6 | energy | 5542 | 1734836 | yes | 1551950 | yes |
| 6 | amplitude | 4993 | 1716719 | yes | 1551950 | yes |
| 8 | energy | 5811 | 1744670 | yes | 1552907 | yes |
| 8 | amplitude | 5173 | 1723616 | yes | 1552907 | yes |

The energy operator loses to the plain amplitude threshold on these
recordings at every SNR.  The generator's background is dominated by small
spikes of distant neurons, whose spectrum matches the target spikes, so the
energy operator's weighting of high frequencies amplifies the background as
much as the signal.  This is a property of the noise model as much as of
the detector; the comparison would need real recordings to generalise.
Precision of both modes falls slightly at the highest SNR; the cause was
not investigated (one candidate is large spikes triggering again on their
rebound after the refractory period).

### 9.3 Compression

| SNR | order 0 | order 1 | order 2 | order-1 bits/sample | order-1 residual entropy (bits) | decoded = input |
|---:|---:|---:|---:|---:|---:|---|
| 2 | 1.313 | 1.855 | 1.729 | 8.63 | 8.41 | yes |
| 3 | 1.313 | 1.854 | 1.729 | 8.63 | 8.41 | yes |
| 4 | 1.313 | 1.854 | 1.729 | 8.63 | 8.42 | yes |
| 5 | 1.313 | 1.853 | 1.729 | 8.63 | 8.42 | yes |
| 6 | 1.312 | 1.853 | 1.729 | 8.64 | 8.42 | yes |
| 8 | 1.312 | 1.852 | 1.729 | 8.64 | 8.43 | yes |

Block size (order 1, SNR 5):

| block (frames) | samples per block | compression ratio |
|---:|---:|---:|
| 8 | 768 | 1.562 |
| 32 | 3072 | 1.785 |
| 128 | 12288 | 1.853 |
| 512 | 49152 | 1.871 |
| 2048 | 196608 | 1.876 |

Rice coding gets within about 0.3 bit per sample of the zeroth-order
entropy of the residual.  The ratio hardly depends on SNR because the
background noise, not the spikes, sets the number of bits per sample:
roughly 75 LSB of in-band noise (15 uV at 0.195 uV per LSB) leaves about
8.5 bits of information per sample.

### 9.4 Throughput, minimum clock and real-time check

| PE | measured cycles/sample (this data) | worst case cycles/sample | why | minimum clock for 96 x 30 kHz (worst case) |
|---|---:|---:|---|---:|
| bp | 13.000 | 13.000 | 1 accept + 11 multiply-accumulate + 1 output cycle, every sample | 37.44 MHz |
| noise | 1.000 | 1.000 | one sample per cycle | 2.88 MHz |
| neo | 1.000 | 1.000 | one sample per cycle | 2.88 MHz |
| snip | 1.056 | 2.500 | 1 per sample + 33 per packet; a channel can start a new packet at most every POST + 1 = 22 frames | 7.20 MHz |
| pred | 1.000 | 1.000 | one sample per cycle | 2.88 MHz |
| rice | 1.000 | 2.000 | 2 cycles if every sample needs an escape code, + 1 flush cycle per block | 5.76 MHz |

Real-time check (ADC-paced input, 3000 frames = 100 ms):

| PE | clock | overruns |
|---|---:|---:|
| bp | 36.317 MHz (0.97 x worst case) | 287192 |
| bp | 38.563 MHz (1.03 x worst case) | 0 |
| noise | 2.794 MHz (0.97 x worst case) | 287331 |
| noise | 2.966 MHz (1.03 x worst case) | 0 |
| neo | 2.794 MHz (0.97 x worst case) | 286836 |
| neo | 2.966 MHz (1.03 x worst case) | 0 |
| snip | lowest without overruns: 4.379 MHz (1.521 cycles/sample) | 0 |
| pred | 2.794 MHz (0.97 x worst case) | 287317 |
| pred | 2.966 MHz (1.03 x worst case) | 0 |
| rice | lowest without overruns: 2.905 MHz (1.009 cycles/sample) | 0 |

Two observations.  First, constant-rate PEs behave exactly as their cycle
count predicts: 3 % below the worst-case clock the input falls behind and
almost every later sample is late; 3 % above, none is.  Second, bursty PEs
need more than their average: the snippet PE averages 1.056 cycles per
sample on this recording but needs a clock of 1.521 cycles per sample to
absorb its bursts with the available FIFOs, and only if its neighbours
drain their backlog quickly (in the bisection all other PEs ran at
50 MHz).  The latency run, with every PE close to its own minimum, needed
more headroom for the snippet, NEO and noise PEs.  This is the FIFO-size
versus frequency trade-off that HALO describes for its bursty compression
PEs.

### 9.5 Latency

Clocks: fab 10 MHz, bp 40 MHz, noise 5 MHz, neo 5 MHz, snip 8 MHz, pred 3.2 MHz, rice 3.6 MHz; no overruns.

| event | count | min (us) | median (us) | max (us) | of which waiting for later samples |
|---|---:|---:|---:|---:|---|
| NEO detection leaves the chip | 492 | 37.0 | 37.4 | 41.0 | 33.3 us (one frame: psi[n] needs x[n+1]) |
| snippet header leaves the chip | 492 | 737.9 | 738.4 | 742.5 | 733.3 us (POST + 1 frames after the peak) |

Almost all of the latency is algorithmic: the energy operator waits one
frame (33.3 us) for x[n+1], and a snippet waits for the 21 samples after its
peak (733 us).  The hardware (FIFOs, clock-domain crossings, PEs at these
clocks) adds the rest.

### 9.6 Resources (Yosys `synth_xilinx`, XC7S50)

Generated by `make synth`.  Each PE is synthesised on its own (NCH = 96), then the whole chip (`spikeline_top`, every PE in its own clock domain).  LUTRAM = distributed RAM primitives (the per-channel state memories and FIFO storage).

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

Where the rest of the whole chip goes (leaf cells by module):

| Part | LUT | LUTRAM | FF |
|---|---:|---:|---:|
| crossbar (7 sources x 9 destinations, 41-bit) | 1,375 | 0 | 9 |
| async FIFOs (12 x 8 entries) | 192 | 84 | 192 |
| port FIFOs (4 x 16 entries) | 52 | 28 | 56 |
| config crossings (6) | 54 | 0 | 414 |
| two-flop synchronisers | 0 | 0 | 228 |
| tile glue (idle detection) | 24 | 0 | 24 |
| registers and counters | 417 | 0 | 350 |
| reset synchronisers | 0 | 0 | 14 |

| Resource | Whole chip | XC7S50 | Share |
|---|---:|---:|---:|
| LUT (logic + LUTRAM) | 4,497 | 32,600 | 13.8% |
| Flip-flops | 1,832 | 65,200 | 2.8% |
| Block RAM (36 Kb) | 4 | 75 | 5.3% |
| DSP48E1 | 7 | 120 | 5.8% |

Everything that provides the flexibility - the crossbar (1,375 LUTs for 9
destinations x 41-bit 8:1 multiplexers), the async FIFOs, the configuration
crossings and the registers - costs about as many LUTs (2,122) as the six
PEs themselves (2,185).  On an FPGA that is the price of reconfigurability
and GALS.
CI's Yosys 0.33 maps the same design to 4,913 LUTs (plus 190 LUTRAM) and 12 DSP48E1 (checked
locally with the `yowasp-yosys` 0.33 build) against the numbers above from
Yosys 0.69.

### 9.7 Switching activity (relative proxy, not power)

Generated by `make activity` (tools/activity.py).  **These are counts of signal transitions in simulation, not a power measurement.**  No open-source tool here can turn them into milliwatts; they only say which blocks switch more than others.

Run: spike detection and compression in parallel, 3000 frames (100 ms) of a synthetic recording (SNR 5) at real time, no overruns.  Clocks: fab 10 MHz, bp 40 MHz, noise 5 MHz, neo 5 MHz, snip 8 MHz, pred 3.2 MHz, rice 3.6 MHz.

| Block | transitions per input sample | share |
|---|---:|---:|
| band-pass PE | 2140.4 | 42.2% |
| ports, registers, counters | 1777.5 | 35.0% |
| band-pass tile boundary (FIFOs, crossings) | 186.2 | 3.7% |
| NEO PE | 173.8 | 3.4% |
| noise tile boundary (FIFOs, crossings) | 106.3 | 2.1% |
| predictor tile boundary (FIFOs, crossings) | 105.6 | 2.1% |
| predictor PE | 100.7 | 2.0% |
| Rice PE | 96.1 | 1.9% |
| NEO tile boundary (FIFOs, crossings) | 89.1 | 1.8% |
| noise PE | 79.8 | 1.6% |
| crossbar | 71.6 | 1.4% |
| Rice tile boundary (FIFOs, crossings) | 55.7 | 1.1% |
| snippet tile boundary (FIFOs, crossings) | 50.9 | 1.0% |
| snippet PE | 40.8 | 0.8% |
| **total** | 5074.6 | 100% |

Clock edges per input sample (rising edges; clock-tree power scales with these):

| Clock | MHz | edges per sample |
|---|---:|---:|
| fab | 10 | 3.47 |
| bp | 40 | 13.89 |
| noise | 5 | 1.74 |
| neo | 5 | 1.74 |
| snip | 8 | 2.78 |
| pred | 3.2 | 1.11 |
| rice | 3.6 | 1.25 |

Reading it: the band-pass PE switches most, as expected for the only PE
that multiplies eleven times per sample.  "Ports, registers, counters"
includes the chip's input pins and the port FIFOs' storage, which the test
harness drives with a new sample every word, and the 32-bit word counters.
The tile boundaries (two async FIFOs and the synchronisers each) cost about
as much switching as the small PEs they wrap - the activity side of the
FPGA-area observation in 9.6.  None of this is a power estimate.

## 10. Comparison with HALO

Published numbers are quoted from the HALO paper in IEEE Micro (2023); they
were not reproduced.  Measured numbers are from this repository.

| | HALO (published) | spikeline (measured) |
|---|---|---|
| input | 96 x 30 kHz x 16 bit, 46 Mbps | same |
| technology | 12 nm tape-out (subset of PEs), 28 nm synthesis; Cadence flow, ST libraries | RTL + simulation; Yosys mapping to a Spartan-7 FPGA |
| PEs | NEO, THR, GATE, LZ, LIC, MA, RC, DWT, TOK, FFT, XCOR, BBF, SVM, AES + RISC-V | band-pass, noise, NEO, snippet, predictor, Rice |
| spike pipeline | ADC -> MUX -> INT -> NEO -> THR -> GATE -> [AES] | in -> band-pass -> noise -> NEO -> snippet |
| compression | LZ4, LZMA (LZ, MA, RC), DWT-based | prediction + adaptive Rice |
| clocking | per-PE clocks 3-180 MHz; NEO 3 MHz (28 nm) / 14 MHz (12 nm) | per-PE minimum 2.88-37.44 MHz (constant-rate PEs); NEO 2.88 MHz |
| interconnect | asynchronous circuit-switched, configured by a RISC-V microcontroller | synchronous crossbar with async FIFOs at each tile, configured over a register bus |
| power | at most 6.3 mW per pipeline (12 nm); 15 mW device budget | not measured |
| area | 832 KGE for the second 12 nm tape-out | 4,307 LUT + 190 LUTRAM + 1,832 FF + 4 BRAM36 + 7 DSP48E1 on an FPGA (not comparable) |

The similar NEO clock is expected rather than meaningful: both process one
sample per cycle.

## 11. Challenges and bugs found

* **Configuration acknowledge race.**  The configuration crossing first
  acknowledged a write on capture.  The PE's idle flag reaches the fabric
  through a different synchroniser, so after a clear command STATUS could
  briefly say "drained" before the clear sweep had started; input sent then
  queued behind the sweep.  The real-time bench showed it as a burst of
  about 2,400 late samples for a PE with 3 % headroom.  Fixed by
  acknowledging only when the word is applied (after the sweep), with a unit
  test and a mutant that reverts the fix.
* **Rice burst.**  The block's first frame (raw samples) became two-cycle
  escapes: 192 cycles of work for 96 samples once per block.  The real-time
  bench reported overruns for a coder 3 % above its average need.  Fixed by
  sending those samples as plain 20-bit values (one cycle, fewer bits).
* **Warm-up burst.**  With a short warm-up the noise estimate had not
  converged when detection switched on; all channels crossed their
  thresholds together and the snippet PE received a burst it could not
  absorb in real time.  The defaults (1024 frames, step 1/16 LSB) avoid it.
* **Mutation tool false kills.**  The first mutation run reported every
  mutant killed because the test command broke on the space in the
  interpreter's path.  A control run on an unchanged copy now has to pass
  every suite before mutants are counted.
* **Fresh-checkout build.**  Verilator did not create `build/` itself;
  found when the mutation tool built a copy without it.

## 12. Lessons learned

* A bit-exact model pays for itself immediately: every PE was compared word
  for word from its first simulation, and long statistics could then be
  computed in numpy instead of in the simulator.
* Functional tests pass at any clock; the bugs that remained were all about
  *time*: which signal arrives first, and how bursty a PE is.  Running at
  the real sample rate near the minimum clock was the test that found them.
* The fixed-point result was not the one expected: the coefficient width
  hardly matters, the filter's state precision does.
* An honest negative result (NEO losing to an amplitude threshold on this
  data) is more useful than a tuned positive one, as long as the reason is
  explained.
* Simulation-based CDC tests check protocols, not physics; knowing what a
  test cannot show matters as much as what it shows.

## 13. Future work

* Real recordings with a clear licence for detection and compression.
* A smoothed NEO and a per-channel adaptive threshold constant.
* Narrower crossbar multiplexers (each destination only needs the bits it
  reads) and an asynchronous handshake fabric instead of FIFOs at every tile.
* Burst-aware FIFO sizing per tile, chosen with the real-time bench.
* Place and route on the XC7S50 and timing analysis; a board test with a
  streamed recording.
* Formal checks of the async FIFO and crossbar.

## 14. Reproducing every number

| Number | Command | Output |
|---|---|---|
| tests, lint | `make test` | console |
| detection, compression, hardware check | `make metrics` | `docs/results/detection.md`, `compression.md` |
| filter sweep | `make quant` | `docs/results/quantisation.md` |
| throughput, real time, latency | `make bench` | `docs/results/throughput.md` |
| resources | `make synth` | `synth/report.md` |
| activity proxy | `make activity` | `docs/results/activity.md` |
| mutation check | `make mutants` | `build/mutants.md` (copied to `docs/mutants.md`) |
| picture | `make demo` | `build/demo/demo.png` |

Machine: Apple M5 (10 cores, 16 GB), macOS 27, Verilator 5.052, Icarus
Verilog 13.0, Yosys 0.69, Python 3.13; shared with other jobs while the
numbers were taken.  Only simulated time and cycle counts are reported, so
the sharing does not change them.
