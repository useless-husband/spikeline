// Spike detector PE: nonlinear energy operator with an adaptive threshold
// and a refractory period.  Bit-exact reference: model/slmodel.py, class Neo.
//
//     psi[n] = x[n]^2 - x[n-1] * x[n+1]
//
// psi grows with both amplitude and frequency, so the sharp spike stands
// out from slower background.  It needs the *next* sample, so the PE keeps
// x[n-1] and x[n] per channel and decides about x[n] when x[n+1] arrives:
// the output stream is the input stream one frame late (the first frame
// after a clear is all zeros), with a detect flag:
//     out = {15'b0, det, x[n]}
//
// Input words come from the noise PE: {ready, noise (Q11.4), x}.
//     energy mode (cfg[24] = 0):    det if psi > (noise^2 * C) >> 16
//     amplitude mode (cfg[24] = 1): det if |x[n]| > (noise * C) >> 12
// and only if ready and the channel is not refractory.  After a detection
// the channel ignores the next R frames.
//
// cfg[15:0] C, cfg[23:16] R, cfg[24] mode.  One sample per cycle.
module pe_neo #(
    parameter int NCH = 96
) (
    input  logic        clk,
    input  logic        rst,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0] cfg,
    /* verilator lint_on UNUSEDSIGNAL */
    input  logic        clr,
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [31:0] in_data,
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

    // per channel: x[n-1], x[n], refractory count
    logic [39:0]   st [0:NCH-1];
    logic          clearing;
    logic [CW-1:0] cidx;

    wire [15:0] c_thr = cfg[15:0];
    wire [7:0]  refr  = cfg[23:16];
    wire        amp_mode = cfg[24];

    wire [CW-1:0] ch = in_tag[CW-1:0];
    wire [39:0] s = st[ch];
    wire [15:0] xp = s[39:24];
    wire [15:0] xc = s[23:8];
    wire [7:0]  r  = s[7:0];
    wire [15:0] xn = in_data[15:0];
    wire [14:0] nz = in_data[30:16];
    wire        rdy = in_data[31];

    // energy test: 33-bit signed psi against a 30-bit threshold
    wire signed [32:0] xp33 = {{17{xp[15]}}, xp};
    wire signed [32:0] xc33 = {{17{xc[15]}}, xc};
    wire signed [32:0] xn33 = {{17{xn[15]}}, xn};
    wire signed [32:0] psi  = xc33 * xc33 - xp33 * xn33;
    wire [29:0] nsq = {15'd0, nz} * {15'd0, nz};
    /* verilator lint_off UNUSEDSIGNAL */
    wire [45:0] thr_e = {16'd0, nsq} * {30'd0, c_thr};
    wire hit_e = psi > $signed({3'b000, thr_e[45:16]});
    // amplitude test
    wire [16:0] axc = xc[15] ? 17'd0 - {1'b1, xc} : {1'b0, xc};
    wire [30:0] thr_a = {16'd0, nz} * {15'd0, c_thr};
    /* verilator lint_on UNUSEDSIGNAL */
    wire hit_a = {2'b00, axc} > thr_a[30:12];

    wire det = rdy && (amp_mode ? hit_a : hit_e) && r == 8'd0;
    wire [7:0] r_next = det ? refr : (r != 8'd0 ? r - 8'd1 : 8'd0);

    assign in_ready = !clearing && (!out_valid || out_ready);
    assign idle = !clearing && !out_valid;
    wire fire = in_valid && in_ready;

    always_ff @(posedge clk)
        if (clearing) st[cidx] <= '0;
        else if (fire) st[ch] <= {xc, xn, r_next};

    always_ff @(posedge clk)
        if (rst || clr) begin
            clearing <= 1'b1;
            cidx <= '0;
            out_valid <= 1'b0;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            if (out_valid && out_ready) out_valid <= 1'b0;
            if (fire) begin
                out_valid <= 1'b1;
                out_data <= {15'd0, det, xc};
                out_tag <= in_tag;
                out_last <= in_last;
            end
        end
endmodule
