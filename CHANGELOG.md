# Changelog

## 0.1.0 - 2026-10-04

First complete version.

- Six streaming processing elements in SystemVerilog: 300-6000 Hz band-pass
  filter (two biquads, one multiplier), running-median noise estimator,
  NEO / amplitude spike detector with refractory period, peak-aligned
  snippet extractor, linear predictor and adaptive Rice coder.
- Configurable crossbar with multicast, register block, and GALS tiles:
  each PE optionally in its own clock domain behind asynchronous FIFOs
  (Gray pointers, two-flop synchronisers) with a handshake for
  configuration.
- Bit-exact numpy golden model, independent decoder, synthetic recording
  generator with ground truth.
- Tests: golden-model tests, cocotb unit tests per PE, async FIFO and
  configuration-crossing stress tests with CDC jitter injection, whole-chip
  Verilator tests (three pipelines, run-time reconfiguration, randomised
  configurations on three builds), mutation check.
- Measurements: detection precision/recall versus SNR, compression ratio,
  filter fixed-point sweep, cycles per sample, minimum clocks with a
  real-time check, latency, Yosys resource use for the XC7S50, switching
  activity proxy.

### Fixed during development (found by the real-time bench)

- A configuration write with the clear bit was acknowledged before the PE's
  clear sweep started, so `STATUS.drained` could be read as set too early.
- The Rice coder turned the first frame of every block into two-cycle
  escape codes, a burst a coder clocked near 2.88 MHz could not absorb;
  those samples are now sent as plain 20-bit values.
