# spikeline -- run every target from the repository root.
#
# Every path below is relative on purpose: the checkout may live in a
# directory whose name contains spaces or non-ASCII characters, which GNU make
# (and Verilator's generated makefiles) cannot handle in absolute paths.  For
# the same reason the Verilator models are compiled with one direct compiler
# call instead of Verilator's own makefile.

SHELL := /bin/bash
VENV ?= .venv
PYTHON ?= $(if $(wildcard $(VENV)/bin/python),$(VENV)/bin/python,python3)
VERILATOR ?= verilator
YOSYS ?= yosys
CXX ?= c++
JOBS ?= 4

RTL := rtl/sl_sync2.sv rtl/sl_rst_sync.sv rtl/sl_fifo.sv rtl/sl_async_fifo.sv rtl/sl_cfg_cdc.sv \
       rtl/pe_bandpass.sv rtl/pe_noise.sv rtl/pe_neo.sv rtl/pe_snippet.sv rtl/pe_predict.sv \
       rtl/pe_rice.sv rtl/sl_tile.sv rtl/sl_xbar.sv rtl/spikeline_top.sv
INC := rtl/bandpass_coefs.svh

VROOT := $(shell $(VERILATOR) --getenv VERILATOR_ROOT 2>/dev/null)
VDEFS := -DVM_SC=0 -DVM_TIMING=0 -DVM_TRACE=0 -DVM_TRACE_FST=0 -DVM_TRACE_VCD=0 -DVM_TRACE_SAIF=0
VFLAGS := --cc -O3 --x-assign fast --x-initial fast --noassert -Irtl --top-module spikeline_top \
          +define+SL_CDC_JITTER -Wno-fatal -Wno-lint -Wno-style

# Simulator variants (ASYNC_MASK: bit p = PE p in its own clock domain)
G_gals  := -GASYNC_MASK=6\'h3F
G_sync  := -GASYNC_MASK=6\'h00
G_mixed := -GASYNC_MASK=6\'h15
G_cov   := -GASYNC_MASK=6\'h3F --coverage-toggle
COV_cov := 1

.PHONY: all lint unit system test check-python venv clean coefs synth demo metrics bench mutants
all: test

.SECONDARY:

# ---------------------------------------------------------------- coefficients
coefs:
	$(PYTHON) tools/gen_coefs.py

# ------------------------------------------------------------------------ lint
lint:
	$(VERILATOR) --lint-only -Wall -Irtl --top-module spikeline_top -GASYNC_MASK=6\'h3F $(RTL)
	$(VERILATOR) --lint-only -Wall -Irtl --top-module spikeline_top -GASYNC_MASK=6\'h00 $(RTL)
	$(VERILATOR) --lint-only -Wall -Irtl --top-module spikeline_top -GASYNC_MASK=6\'h15 $(RTL)
	@echo "lint: verilator -Wall clean (GALS, synchronous and mixed builds)"

# ----------------------------------------------------------------------- tests
check-python:
	@$(PYTHON) -c "import cocotb, pytest, numpy, scipy" 2>/dev/null || { \
	  echo "cocotb/pytest/numpy/scipy not found for $(PYTHON)."; \
	  echo "Run 'make venv' once (creates $(VENV)), or pass PYTHON=/path/to/python."; exit 1; }

unit: check-python
	$(PYTHON) -m pytest -q tests/model tests/unit

build/vm_%/Vspikeline_top.h: $(RTL) $(INC)
	rm -rf build/vm_$*
	$(VERILATOR) $(VFLAGS) $(G_$*) -Mdir build/vm_$* $(RTL)

build/vsim_%: build/vm_%/Vspikeline_top.h sim/sim_main.cpp
	$(CXX) -std=c++17 -O2 -w $(VDEFS) -DVM_COVERAGE=$(if $(COV_$*),1,0) \
	  -Ibuild/vm_$* -I$(VROOT)/include -I$(VROOT)/include/vltstd \
	  build/vm_$*/*.cpp $(VROOT)/include/verilated.cpp $(VROOT)/include/verilated_threads.cpp \
	  $(if $(COV_$*),$(VROOT)/include/verilated_cov.cpp) sim/sim_main.cpp -o $@ -lpthread

SIMS := build/vsim_gals build/vsim_sync build/vsim_mixed

system: check-python $(SIMS)
	$(PYTHON) -m pytest -q tests/system

test: lint unit system

# ----------------------------------------------------------------------- setup
venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt

clean:
	rm -rf build
