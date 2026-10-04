// Multi-clock Verilator harness for spikeline_top.
//
// Seven independent clocks (fabric + one per PE tile), each with its own
// period, a random start phase and optional per-edge jitter, are advanced
// event by event: the harness always jumps to the next clock edge, so
// unrelated frequencies interleave exactly as they would in time.
//
// The harness plays the rest of the system in the fabric clock domain:
//   - an ADC / sample source on the input port (as fast as the chip accepts,
//     or one sample every --adc-period ps like a real converter),
//   - three sinks on the output ports with random ready (backpressure),
//   - a small sequencer that runs a script of register writes and reads.
//
// Script commands (one per line, # comments):
//   w ADDR VALUE     write a register (waits while the chip stalls the bus)
//   r ADDR           read a register and print "r ADDR VALUE"
//   expect ADDR VAL  read and fail unless equal
//   feed N           send the next N frames of the input file, wait until accepted
//   drain            wait until STATUS.drained (bit 16) is set
//   wait N           wait N fabric cycles
//   mark NAME        print "mark NAME <time> <output records so far>"
// Numbers may be decimal or 0x hex.
//
// Output: every word leaving an output port is appended to --out as a
// 16-byte record {u32 data, u8 tag, u8 last, u8 port, u8 0, u64 time_ps}.
#include "Vspikeline_top.h"
#include "verilated.h"

#include <cinttypes>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#if VM_COVERAGE
#include "verilated_cov.h"
#endif

double sc_time_stamp() { return 0; }  // required by verilated.cpp; time lives in the context

namespace {

struct Clock {
    const char* name;
    uint64_t period;
    uint64_t next;
    int value;
};

struct Cmd {
    std::string op;
    uint64_t a = 0, b = 0;
    std::string text;
};

uint64_t num(const std::string& s) { return std::strtoull(s.c_str(), nullptr, 0); }

[[noreturn]] void die(const char* msg) {
    std::fprintf(stderr, "sim: %s\n", msg);
    std::exit(2);
}

std::vector<Cmd> read_script(const char* path) {
    std::vector<Cmd> cmds;
    FILE* f = std::fopen(path, "r");
    if (!f) die("cannot open script");
    char line[512];
    while (std::fgets(line, sizeof line, f)) {
        char* hash = std::strchr(line, '#');
        if (hash) *hash = 0;
        char op[32] = {0}, a[128] = {0}, b[128] = {0};
        int n = std::sscanf(line, "%31s %127s %127s", op, a, b);
        if (n < 1) continue;
        Cmd c;
        c.op = op;
        if (c.op == "mark") c.text = a;
        else {
            if (n >= 2) c.a = num(a);
            if (n >= 3) c.b = num(b);
        }
        cmds.push_back(c);
    }
    std::fclose(f);
    return cmds;
}

}  // namespace

int main(int argc, char** argv) {
    const char* in_path = nullptr;
    const char* script_path = nullptr;
    const char* out_path = nullptr;
    const char* cov_path = nullptr;
    int nch = 96;
    uint64_t seed = 1, jitter = 0, adc_period = 0, max_time = 0;
    double in_prob = 1.0, ready_p[3] = {1.0, 1.0, 1.0};
    Clock clocks[7] = {{"fab", 20000, 0, 0},  {"bp", 20000, 0, 0},   {"noise", 20000, 0, 0},
                       {"neo", 20000, 0, 0},  {"snip", 20000, 0, 0}, {"pred", 20000, 0, 0},
                       {"rice", 20000, 0, 0}};

    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto val = [&]() -> const char* {
            if (i + 1 >= argc) die("missing value");
            return argv[++i];
        };
        if (a == "--in") in_path = val();
        else if (a == "--script") script_path = val();
        else if (a == "--out") out_path = val();
        else if (a == "--coverage") cov_path = val();
        else if (a == "--nch") nch = std::atoi(val());
        else if (a == "--seed") seed = num(val());
        else if (a == "--jitter") jitter = num(val());
        else if (a == "--adc-period") adc_period = num(val());
        else if (a == "--max-time") max_time = num(val());
        else if (a == "--in-prob") in_prob = std::atof(val());
        else if (a == "--ready") {
            if (std::sscanf(val(), "%lf,%lf,%lf", &ready_p[0], &ready_p[1], &ready_p[2]) != 3)
                die("--ready wants P0,P1,P2");
        } else if (a == "--period") {
            // --period name=ps
            std::string kv = val();
            size_t eq = kv.find('=');
            if (eq == std::string::npos) die("--period wants name=ps");
            bool found = false;
            for (auto& c : clocks)
                if (kv.substr(0, eq) == c.name) {
                    c.period = num(kv.substr(eq + 1));
                    found = true;
                }
            if (!found) die("unknown clock name");
        } else if (a.rfind("+verilator", 0) == 0) {
            // passed through to Verilator below
        } else {
            std::fprintf(stderr, "unknown option %s\n", a.c_str());
            return 2;
        }
    }
    if (!script_path || !out_path) die("need --script and --out");

    std::vector<int16_t> samples;
    if (in_path) {
        FILE* f = std::fopen(in_path, "rb");
        if (!f) die("cannot open input");
        int16_t buf[4096];
        size_t n;
        while ((n = std::fread(buf, 2, 4096, f)) > 0) samples.insert(samples.end(), buf, buf + n);
        std::fclose(f);
    }
    std::vector<Cmd> cmds = read_script(script_path);
    FILE* out = std::fopen(out_path, "wb");
    if (!out) die("cannot open output");

    auto ctx = std::make_unique<VerilatedContext>();
    ctx->randSeed(static_cast<int>(seed & 0x7fffffff));
    ctx->commandArgs(argc, argv);
    auto top = std::make_unique<Vspikeline_top>(ctx.get());

    std::mt19937_64 rng(seed);
    std::uniform_real_distribution<double> uni(0.0, 1.0);
    for (auto& c : clocks) {
        if (c.period < 4) die("clock period too small");
        c.next = 1 + rng() % c.period;   // random phase
    }
    auto half = [&](const Clock& c) -> uint64_t {
        uint64_t h = c.period / 2;
        if (jitter) h = h - jitter + rng() % (2 * jitter + 1);
        return h < 1 ? 1 : h;
    };
    auto set_clocks = [&]() {
        top->clk_fab = clocks[0].value;
        top->clk_bp = clocks[1].value;
        top->clk_noise = clocks[2].value;
        top->clk_neo = clocks[3].value;
        top->clk_snip = clocks[4].value;
        top->clk_pred = clocks[5].value;
        top->clk_rice = clocks[6].value;
    };

    // ------------------------------------------------------------ state
    uint64_t now = 0, fab_cycles = 0, records = 0, overruns = 0, max_late = 0;
    size_t pc = 0;                       // script position
    uint64_t wait_left = 0;
    size_t word = 0, word_limit = 0;     // input words sent / allowed
    uint64_t adc_base_time = 0;
    size_t adc_base_word = 0;
    bool reset_done = false;
    int errors = 0;

    top->arst = 1;
    top->cfg_valid = 0;
    top->in_valid = 0;
    top->out_ready = 0;
    set_clocks();
    top->eval();

    auto in_word_ready = [&](size_t k) -> bool {
        if (k >= word_limit || k >= samples.size()) return false;
        if (!adc_period) return true;
        return now >= adc_base_time + (k - adc_base_word) * adc_period;
    };

    while (true) {
        if (max_time && now > max_time) {
            std::fprintf(stderr, "sim: time limit reached at script line %zu (%s)\n", pc,
                         pc < cmds.size() ? cmds[pc].op.c_str() : "end");
            errors++;
            break;
        }
        // next edge
        uint64_t t = UINT64_MAX;
        for (auto& c : clocks) t = c.next < t ? c.next : t;
        now = t;
        bool fab_rise = false;
        for (auto& c : clocks)
            if (c.next == t) {
                c.value ^= 1;
                c.next += half(c);
                if (&c == &clocks[0] && c.value) fab_rise = true;
            }

        // handshakes are decided by the values just before the fabric edge
        bool in_fire = false, cfg_fire = false;
        uint32_t cfg_rdata = 0;
        if (fab_rise && reset_done) {
            in_fire = top->in_valid && top->in_ready;
            for (int k = 0; k < 3; k++)
                if (((top->out_valid >> k) & 1) && ((top->out_ready >> k) & 1)) {
                    uint8_t rec[16] = {0};
                    uint32_t d = top->out_data[k];
                    std::memcpy(rec, &d, 4);
                    rec[4] = (top->out_tag >> (8 * k)) & 0xff;
                    rec[5] = (top->out_last >> k) & 1;
                    rec[6] = static_cast<uint8_t>(k);
                    std::memcpy(rec + 8, &now, 8);
                    std::fwrite(rec, 1, 16, out);
                    records++;
                }
            cfg_fire = top->cfg_valid && top->cfg_ready;
            cfg_rdata = top->cfg_rdata;
        }

        set_clocks();
        top->eval();
        if (!fab_rise) continue;
        fab_cycles++;

        // ---------------------------------------------- fabric-side harness
        if (!reset_done) {
            if (fab_cycles == 8) top->arst = 0;
            if (fab_cycles >= 16) reset_done = true;
            top->eval();
            continue;
        }
        if (in_fire) {
            if (adc_period) {
                uint64_t deadline = adc_base_time + (word - adc_base_word + 1) * adc_period;
                if (now > deadline) {
                    overruns++;
                    if (now - deadline > max_late) max_late = now - deadline;
                }
            }
            word++;
        }
        bool offer = in_word_ready(word) && (adc_period || uni(rng) < in_prob);
        top->in_valid = offer;
        if (word < samples.size()) {
            int ch = static_cast<int>(word % nch);
            top->in_data = static_cast<uint32_t>(static_cast<int32_t>(samples[word]));
            top->in_tag = ch;
            top->in_last = ch == nch - 1;
        }
        uint8_t ready = 0;
        for (int k = 0; k < 3; k++)
            if (uni(rng) < ready_p[k]) ready |= 1 << k;
        top->out_ready = ready;

        // sequencer
        top->cfg_valid = 0;
        top->cfg_we = 0;
        while (pc < cmds.size()) {
            Cmd& c = cmds[pc];
            if (c.op == "w") {
                // a write is in flight if we drove it last cycle and it was not accepted
                static bool driving = false;
                if (driving && cfg_fire) {
                    driving = false;
                    pc++;
                    continue;
                }
                driving = true;
                top->cfg_valid = 1;
                top->cfg_we = 1;
                top->cfg_addr = static_cast<uint8_t>(c.a);
                top->cfg_wdata = static_cast<uint32_t>(c.b);
                break;
            } else if (c.op == "r" || c.op == "expect" || c.op == "drain") {
                static bool reading = false;
                if (reading && cfg_fire) {
                    reading = false;
                    if (c.op == "r") {
                        std::printf("r 0x%02" PRIx64 " 0x%08x\n", c.a, cfg_rdata);
                    } else if (c.op == "expect") {
                        if (cfg_rdata != static_cast<uint32_t>(c.b)) {
                            std::fprintf(stderr, "sim: expect 0x%02" PRIx64 ": got 0x%08x want 0x%08" PRIx64 "\n",
                                         c.a, cfg_rdata, c.b);
                            errors++;
                        }
                    } else if (!(cfg_rdata & (1u << 16)) || in_word_ready(word) || word < word_limit) {
                        // not drained yet: poll again
                        reading = true;
                        top->cfg_valid = 1;
                        top->cfg_addr = 0x02;
                        break;
                    }
                    pc++;
                    continue;
                }
                reading = true;
                top->cfg_valid = 1;
                top->cfg_addr = static_cast<uint8_t>(c.op == "drain" ? 0x02 : c.a);
                break;
            } else if (c.op == "feed") {
                static bool fed = false;
                if (!fed) {
                    fed = true;
                    word_limit = word + c.a * nch;
                    if (word_limit > samples.size()) die("feed beyond the end of the input file");
                    adc_base_time = now;
                    adc_base_word = word;
                    std::printf("feed %zu %" PRIu64 "\n", word, now);
                }
                if (word >= word_limit) {
                    fed = false;
                    pc++;
                    continue;
                }
                break;
            } else if (c.op == "wait") {
                static bool waiting = false;
                if (!waiting) {
                    waiting = true;
                    wait_left = c.a;
                }
                if (wait_left == 0) {
                    waiting = false;
                    pc++;
                    continue;
                }
                wait_left--;
                break;
            } else if (c.op == "mark") {
                std::printf("mark %s %" PRIu64 " %" PRIu64 "\n", c.text.c_str(), now, records);
                pc++;
            } else {
                std::fprintf(stderr, "sim: unknown command %s\n", c.op.c_str());
                return 2;
            }
        }
        if (pc >= cmds.size() && !in_word_ready(word)) break;
        top->eval();
    }

    std::fclose(out);
    std::printf("done time_ps %" PRIu64 " fab_cycles %" PRIu64 " in_words %zu out_records %" PRIu64
                " overruns %" PRIu64 " max_late_ps %" PRIu64 "\n",
                now, fab_cycles, word, records, overruns, max_late);
#if VM_COVERAGE
    if (cov_path) VerilatedCov::write(cov_path);
#else
    (void)cov_path;
#endif
    top->final();
    return errors ? 1 : 0;
}
