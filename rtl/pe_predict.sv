// Linear predictor PE (first half of the lossless compressor).  Bit-exact
// reference: model/slmodel.py, class Predictor.
//
// Per channel: e = x (order 0), x - x[n-1] (order 1) or x - 2x[n-1] + x[n-2]
// (order 2).  Samples are grouped in blocks of 2^B frames; frame fb of a
// block uses order min(order, fb), so no prediction reaches into the
// previous block and every block can be decoded on its own (a lost radio
// packet costs one block, not the rest of the recording).
//
//     out = {lastblk, first, 10'b0, e[19:0]}
// first = first frame of a block, lastblk = final sample of a block.
//
// cfg[1:0] order (3 acts as 2), cfg[5:2] B.  One sample per cycle.
module pe_predict #(
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

    logic [31:0]   st [0:NCH-1];   // x[n-1], x[n-2]
    logic [14:0]   fb;             // frame index inside the block
    logic          clearing;
    logic [CW-1:0] cidx;

    wire [1:0]  order = cfg[1:0];
    wire [3:0]  blog  = cfg[5:2];
    wire [14:0] mask  = ~(15'h7FFF << blog);   // 2^B - 1

    wire [CW-1:0] ch = in_tag[CW-1:0];
    wire [15:0] x  = in_data[15:0];
    wire [15:0] x1 = st[ch][31:16];
    wire [15:0] x2 = st[ch][15:0];

    wire [19:0] x_e  = {{4{x[15]}}, x};
    wire [19:0] x1_e = {{4{x1[15]}}, x1};
    wire [19:0] x2_e = {{4{x2[15]}}, x2};
    wire        use1 = fb != 15'd0 && order != 2'd0;
    wire        use2 = fb > 15'd1 && order[1];
    wire [19:0] e = use2 ? x_e - {x1_e[18:0], 1'b0} + x2_e : (use1 ? x_e - x1_e : x_e);
    wire        first = fb == 15'd0;
    wire        lastblk = fb >= mask && in_last;

    assign in_ready = !clearing && (!out_valid || out_ready);
    assign idle = !clearing && !out_valid;
    wire fire = in_valid && in_ready;

    always_ff @(posedge clk)
        if (clearing) st[cidx] <= '0;
        else if (fire) st[ch] <= {x, x1};

    always_ff @(posedge clk)
        if (rst || clr) begin
            clearing <= 1'b1;
            cidx <= '0;
            fb <= '0;
            out_valid <= 1'b0;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            if (out_valid && out_ready) out_valid <= 1'b0;
            if (fire) begin
                out_valid <= 1'b1;
                out_data <= {lastblk, first, 10'd0, e};
                out_tag <= in_tag;
                out_last <= in_last;
                if (in_last) fb <= fb >= mask ? 15'd0 : fb + 15'd1;
            end
        end
endmodule
