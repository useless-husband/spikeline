// Noise estimator PE: per-channel running median of |x| (a hardware-cheap
// stand-in for the median absolute deviation).  Bit-exact reference:
// model/slmodel.py, class Noise.
//
// m (Q16.8, 24 bits) steps up by `step` when |x| is above it and down when
// below, so it settles at the median of |x|; spikes are rare, so they barely
// move it.  Each sample leaves with the estimate *before* its own update:
//     out = {ready, noise[14:0] = sat(m >> 4), x[15:0]}
// ready = at least `warmup` frames seen since the last clear.
//
// cfg[7:0] step (1/256 LSB per sample), cfg[23:8] warmup frames.
// One sample per cycle.
module pe_noise #(
    parameter int NCH = 96
) (
    input  logic        clk,
    input  logic        rst,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0] cfg,
    input  logic [31:0] in_data,
    /* verilator lint_on UNUSEDSIGNAL */
    input  logic        clr,
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [7:0]  in_tag,
    input  logic        in_last,
    output logic        out_valid,
    input  logic        out_ready,
    output logic [31:0] out_data,
    output logic [7:0]  out_tag,
    output logic        out_last,
    output logic        idle
);
    localparam int CW = $clog2(NCH);
    localparam int NCH_M1 = NCH - 1;
    localparam logic [CW-1:0] LAST_CH = NCH_M1[CW-1:0];

    logic [23:0]   m [0:NCH-1];
    logic [15:0]   fcnt;
    logic          clearing;
    logic [CW-1:0] cidx;

    wire [7:0]  step   = cfg[7:0];
    wire [15:0] warmup = cfg[23:8];
    wire [CW-1:0] ch   = in_tag[CW-1:0];
    wire [15:0] x      = in_data[15:0];
    wire [23:0] mc     = m[ch];

    wire [16:0] ax = x[15] ? 17'd0 - {1'b1, x} : {1'b0, x};       // |x|, up to 32768
    wire [24:0] a  = {ax, 8'd0};
    wire [24:0] up = {1'b0, mc} + {17'd0, step};
    wire [23:0] m_up = up[24] ? 24'hFFFFFF : up[23:0];
    wire [23:0] m_dn = mc > {16'd0, step} ? mc - {16'd0, step} : 24'd0;
    wire [23:0] m_next = a > {1'b0, mc} ? m_up : (a < {1'b0, mc} ? m_dn : mc);
    wire [14:0] noise = mc[23:19] != 5'd0 ? 15'h7FFF : mc[18:4];
    wire        ready = fcnt >= warmup;

    assign in_ready = !clearing && (!out_valid || out_ready);
    assign idle = !clearing && !out_valid;
    wire fire = in_valid && in_ready;

    always_ff @(posedge clk)
        if (clearing) m[cidx] <= '0;
        else if (fire) m[ch] <= m_next;

    always_ff @(posedge clk)
        if (rst || clr) begin
            clearing <= 1'b1;
            cidx <= '0;
            fcnt <= '0;
            out_valid <= 1'b0;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            if (out_valid && out_ready) out_valid <= 1'b0;
            if (fire) begin
                out_valid <= 1'b1;
                out_data <= {ready, noise, x};
                out_tag <= in_tag;
                out_last <= in_last;
                if (in_last && fcnt != 16'hFFFF) fcnt <= fcnt + 1'b1;
            end
        end
endmodule
