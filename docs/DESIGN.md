# Design notes

This file explains the architecture, the problems that took real thought,
and the alternatives that were considered and rejected.  The course-style
write-up with all measurements is [report.md](report.md).

## Architecture in one paragraph

Samples arrive as one time-multiplexed stream: frame *f* carries one 16-bit
sample for each of 96 channels, in channel order (`tag` = channel, `last`
on channel 95).  Six processing elements (PEs) each do one job on that
stream and keep their per-channel state in a small memory indexed by the
tag.  A crossbar in the fabric clock domain connects the input port, the PE
outputs and three output ports; one route register per destination says
which source feeds it, so software builds pipelines by writing registers.
Each PE sits in a *tile* that can put it in its own clock domain behind two
asynchronous FIFOs (GALS), so every PE runs at the lowest clock its job
needs.

```
in port -> [crossbar] <-> tile(band-pass) tile(noise) tile(NEO) tile(snippet) tile(predictor) tile(Rice)
                |
                +-> out0, out1, out2
```

## One stream format for everything

Every crossbar port carries the same 41-bit word: `{last, tag[7:0],
data[31:0]}` with valid/ready flow control.  Each PE reads the fields it
needs from `data` and writes its result in a documented layout:

| producer | data[31:0] |
|---|---|
| input port, band-pass | sign-extended sample |
| noise estimator | `{ready, noise[14:0], x[15:0]}` |
| NEO detector | `{15'b0, det, x[15:0]}` (one frame late) |
| snippet | header `{peak frame}`, then 32 sign-extended samples |
| predictor | `{lastblk, first, 10'b0, e[19:0]}` |
| Rice coder | packed bitstream, `tag` = block number |

Carrying the noise estimate *with* each sample, instead of in a side
channel, is what lets the noise estimator be its own PE: the NEO PE does
not need a second input port, and the crossbar stays a plain
one-source-per-destination switch.  The same trick carries the detect flag
from NEO to the snippet PE.

Rejected: a separate narrow bus per PE type.  It would save some crossbar
width, but the crossbar could then only connect type-compatible ports and
probing an internal stream on an output port (used by the demo and the
tests) would need special wiring.

## Time-multiplexed channels and per-channel state

96 channels share each PE.  A PE keeps everything that depends on a
channel's past (filter history, median tracker, NEO history and refractory
counter, snippet capture state, predictor history, Rice accumulator) in a
96-entry memory addressed by `tag`.  Each sample is a read-modify-write of
one entry.  Consecutive samples are different channels, so there is no
read-after-write hazard inside a frame; the band-pass PE, which takes 13
cycles per sample, writes back before it accepts the next sample.

After reset or a `clear` command each PE walks its memory writing zeros
(NCH cycles) before it accepts input; the snippet ring buffer is not
cleared, which is safe because a detection is ignored until PRE frames
after the clear, so a window never reaches back past it.

## The band-pass filter and fixed point

Two direct-form-I biquads (4th-order Butterworth, 300-6000 Hz) computed
with **one** 24 x 18 multiplier (one DSP48E1) in 13 cycles per sample.  The
alternative, ten multipliers and one cycle per sample, would cut the clock
from 37.4 MHz to 2.9 MHz; with GALS the PE simply gets a faster clock, and
the area stays small.

Direct form I was chosen over the transposed direct form II that floating
point code usually uses: with an exact (48-bit) accumulator there is only
one rounding per section, and no internal node can overflow; the price is
storing input history as well as output history (six values per channel,
because section 1's input history is section 0's output history).

The high-pass section has its poles at radius 0.957, close to the unit
circle, so the rounding of its output is amplified by its feedback.  The
measured sweep in [results/quantisation.md](results/quantisation.md) shows
that the *state* precision matters far more than the coefficient
precision: with no fractional state bits the output error is about 10 LSB
rms whatever the coefficients; with six fractional bits it is 0.33 LSB,
close to the 0.29 LSB that rounding the output to whole LSBs costs by
itself.  The chip uses 16 coefficient fractional bits and 6 state
fractional bits.  The high-pass numerator (1, -2, 1) is exact in this
format, so the zero at DC is exact and the electrode offset is removed
completely.

## Noise estimate without sorting

The usual threshold is k x sigma with sigma = median(|x|) / 0.6745
(Quiroga et al. 2004): a median ignores the spikes that an RMS would
count.  A true median needs a window and a sort.  The noise PE instead keeps
one 24-bit number per channel and moves it a small step up when |x| is
above it and down when below; it settles where half the samples are above,
i.e. at the median.  The step (default 1/16 LSB per sample) trades
convergence time against wobble.  A warm-up counter keeps detection off
until the estimate has had time to converge; the defaults (step 16, 1024
frames) reach a median of 64 LSB in time.  A shorter warm-up than the
convergence time makes every channel cross its too-low threshold at once
when warm-up ends - the real-time test showed this as a burst that a
slowly clocked snippet PE cannot absorb.

## Energy versus amplitude detection

The NEO PE implements the nonlinear energy operator psi[n] = x[n]^2 -
x[n-1] x[n+1] with threshold C x noise^2 (C calibrated), a refractory period,
and - for comparison - a plain amplitude mode, |x| > C' x noise, selected by
one configuration bit.  On the synthetic recordings the amplitude mode
detects better (F1 0.95 against 0.79 at SNR 5).  The generator's background
is mostly small spikes of distant neurons, which have the same spectrum as
the spikes being detected, so the energy operator's emphasis on high
frequencies does not separate them; NEO's advantage is known to depend on
that spectral difference.  The energy mode stays the default because it is
the operator this project set out to reproduce; the result is reported as
measured.

## Snippets: alignment without a second pass

A detection fires on the rising edge of a spike, before its peak.  The
snippet PE keeps the last 32 samples of every channel in a ring buffer
(block RAM, address `{channel, frame mod 32}`), searches the next 10 samples
for the largest |x|, and sends the 32-sample window around that peak once
the 21 samples after it have arrived.  Nothing is buffered per spike: the
window is read straight out of the ring.  The ring's read port is fed the
*next* address every cycle, so the data for the current address is always
ready and a packet streams at one word per cycle.

## Lossless compression

Predictor and coder are separate PEs, so the predictor can also feed on the
filtered or delayed streams (the randomised tests do that), and the coder
does not need to know the channel structure beyond the tag.

* **Blocks.**  Every 2^B frames (default 128 = 4.3 ms, 12,288 samples) the
  predictor restarts (prediction order ramps 0, 1, 2) and the coder resets
  its adaptation and pads to a word boundary, so each block decodes on its
  own: a lost radio packet costs one block.  The block-size sweep in
  [results/compression.md](results/compression.md) shows the price of
  short blocks.
* **Adaptive Rice.**  Per channel, A tracks 16 x the mean of the zigzag
  residual (A <- A - A/16 + u) and k = floor(log2(A/16)), the
  LOCO-I/JPEG-LS idea.  Codes longer than 31 bits are escaped (16 ones, then
  20 raw bits), so a code is at most two chunks and the bit packer appends
  one chunk per cycle to a 64-bit buffer, emitting at most one word per
  chunk.
* **First frame of a block.**  The first version coded the restarted
  predictor's raw samples like any residual: almost all became two-cycle
  escapes, a 192-cycle burst per block.  At a clock just above 2.88 MHz the
  FIFOs could not absorb it and the real-time test reported overruns.  These
  samples are now sent as plain 20-bit values in one cycle - shorter too.
  They still update the running mean: a first attempt reset it after the
  first frame instead, which started every block with too small a k and
  lowered the compression ratio from 1.830 to 1.818; seeding it from the
  data raised it to 1.853.
* **Proof of losslessness.**  `model/sl_decode.py` is written separately
  from the encoder model; the tests decode the words the *hardware* emitted
  and compare them with the input.

Rejected: LZ-style dictionary compression (HALO has LZ4/LZMA PEs).  Raw
neural samples are dominated by noise in their low bits and rarely repeat
exactly, so a predictor plus an entropy coder fits the data better and needs
no dictionary memory.

## GALS: crossing clock domains

* **Asynchronous FIFO** (`rtl/sl_async_fifo.sv`): binary and Gray pointers
  on each side, only the Gray pointers cross through two-flop
  synchronisers, full/empty computed from the *next* pointer and
  registered.  A synchronised pointer is always old, which only makes each
  side conservative.
* **Configuration** (`rtl/sl_cfg_cdc.sv`): a toggle handshake.  The 32 data
  bits never change while the PE side samples them, so only the single-bit
  request and acknowledge toggles need synchronisers.  The acknowledge is
  sent only when the PE has *applied* the word - for a clear command, after
  its clear sweep.  The first version acknowledged on capture, and because
  the PE's idle flag crossed through a different synchroniser, `STATUS`
  could say "drained" just before the clear began.  The real-time bench
  exposed it; the fix and a unit test for it are in the history.
* **Reset**: one asynchronous reset input, a reset synchroniser per domain
  (asynchronous assert, synchronous release).
* **Idle detection for reconfiguration**: a tile reports idle only after the
  PE-side view (through a synchroniser) and the fabric-side view of both
  FIFOs have agreed for 7 consecutive cycles, because the two views travel
  through different synchronisers and can arrive one cycle apart.

**Simulating CDC.**  A simulator has no metastability.  With
`SL_CDC_JITTER` defined, every synchroniser randomly captures a bit that has
just changed one cycle late, which is what metastability can do.  The FIFO
and configuration tests run with it.  An honest note from doing this: the
same tests also pass with *binary* pointers crossing (as long as both sides
are consistent), because with registered flags and one transfer per cycle a
one-cycle inconsistent pointer can only expose a slot that is already
written.  The Gray-code design is kept because it is correct by
construction for any logic that uses the pointers (an occupancy count, for
example); the tests demonstrate the protocol, not the absence of every CDC
hazard.  The tests do catch a full-flag formula that is wrong for Gray
pointers and a write pointer that crosses in binary while the reader
expects Gray (see [mutants.md](mutants.md)).

## Crossbar

An 8:1 multiplexer per destination selected by its route register, and an
eager fork for multicast: a destination that has taken the current word sets
its `done` bit; the source advances when every listening destination is
done or ready.  Unrouted sources stall rather than drop data.  Because every
tile input is a FIFO with a registered ready, no combinational path crosses
a tile, so even a routing loop cannot create a combinational cycle.

The synthesis report shows the cost of this flexibility on an FPGA: the
41-bit crossbar alone uses about 1,400 LUTs, and with the FIFOs,
configuration crossings and registers the infrastructure costs about as
many LUTs as the six PEs themselves.  HALO
avoids a comparable cost on silicon with an asynchronous circuit-switched
fabric.  Narrowing each destination's mux to the bits it reads (an estimated
15 % of the crossbar, not measured) was not done.

## Verification strategy

* A bit-exact numpy model of every PE (`model/slmodel.py`), tested on its own
  (hand-computed bitstream, decoder round trips, filter versus floating point,
  median tracker versus the true median, NEO versus the formula).
* cocotb unit tests (Icarus) per PE with random valid/ready gaps, compared
  word for word with the model; throughput assertions (13 cycles, 1 cycle,
  escapes at most 2).
* Async FIFO and configuration-crossing stress tests across clock ratios
  from 1:1 to 1:30 in both directions, random phase, CDC jitter injection.
* Whole-chip tests in Verilator with a multi-clock harness: three pipelines,
  run-time reconfiguration, and 24 randomised configurations (routing, PE
  settings, clock periods and phases, jitter, backpressure, three builds),
  every output word compared with the model.
* Mutation check: 19 one-line bugs, each must make a suite fail.
* Real-time bench: the input paced like a real ADC, overruns counted.  It
  found two design bugs (config acknowledge race, Rice burst) that the
  functional tests could not, because they only show up when the chip runs
  close to its minimum clock.  Two short ADC-paced tests in
  `tests/system/test_realtime.py` now guard against both in `make test`.
