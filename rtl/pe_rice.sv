// Adaptive Rice coder PE (second half of the lossless compressor).
// Bit-exact reference: model/slmodel.py, class Rice; independent decoder:
// model/sl_decode.py.
//
// Input: the predictor's stream {lastblk, first, e[19:0]}.
//   u = zigzag(e)            (0, -1, 1, -2, ... -> 0, 1, 2, 3, ...)
//   A = per-channel running sum, about 16 x mean(u); reset to A_INIT at the
//       first frame of a block; after coding, A <- A - A/16 + u
//   k = floor(log2(A >> 4)), 0 if that is 0, at most 15
//   q = u >> k
//   code = q ones, one zero, the low k bits of u       (q < 16: <= 31 bits)
//        = 16 ones, then u in 20 bits                   (q >= 16: escape)
// Codes are packed MSB-first into 32-bit words.  After the final sample of
// a block the packer pads with zeros to a word boundary and marks that word
// `last`; tag = block number (mod 256).
//
// The packer appends at most one chunk of <= 31 bits per cycle to a 64-bit
// buffer that always holds < 32 bits between cycles, so a chunk produces at
// most one output word.  Normal samples take one cycle; an escape takes two
// (16 ones, then the raw value); a block end may add one flush cycle.
module pe_rice #(
    parameter int NCH = 96
) (
    input  logic        clk,
    input  logic        rst,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0] cfg,       // no run-time settings
    input  logic [31:0] in_data,   // [31] lastblk, [30] first, [19:0] e
    input  logic        in_last,
    input  logic [7:0]  in_tag,    // channel
    /* verilator lint_on UNUSEDSIGNAL */
    input  logic        clr,
    input  logic        in_valid,
    output logic        in_ready,
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
    localparam logic [23:0] A_INIT = 24'd256;

    logic [23:0]   acc [0:NCH-1];
    logic          clearing;
    logic [CW-1:0] cidx;

    localparam logic [1:0] S_IN = 2'd0, S_ESC = 2'd1, S_FLUSH = 2'd2;
    logic [1:0]  state;
    logic [63:0] bits;      // left-aligned: bits[63 -: cnt] are pending
    logic [5:0]  cnt;       // always < 32 between cycles
    logic [7:0]  seq;
    logic [19:0] esc_u;
    logic        esc_final;

    // -------------------------------------------------------------- coding
    wire [CW-1:0] ch = in_tag[CW-1:0];
    wire [19:0] e       = in_data[19:0];
    wire        first   = in_data[30];
    wire        lastblk = in_data[31];
    wire [23:0] a_cur   = first ? A_INIT : acc[ch];
    wire [19:0] m       = a_cur[23:4];
    logic [3:0] k;
    always_comb begin
        k = 4'd0;
        for (int i = 1; i < 16; i++)
            if (m >= (20'd1 << i)) k = k + 4'd1;
    end
    wire [19:0] u      = {e[18:0], 1'b0} ^ {20{e[19]}};
    wire [19:0] q      = u >> k;
    wire        esc    = q[19:4] != 16'd0;
    wire [31:0] ones   = (32'd1 << q[3:0]) - 32'd1;
    wire [31:0] kmask  = (32'd1 << k) - 32'd1;
    wire [31:0] code   = (ones << ({1'b0, k} + 5'd1)) | ({12'd0, u} & kmask);
    wire [5:0]  codelen = {2'b00, q[3:0]} + {2'b00, k} + 6'd1;
    wire [23:0] a_next = a_cur - {4'd0, a_cur[23:4]} + {4'd0, u};

    // -------------------------------------------------------------- packer
    logic [31:0] ch_val;
    logic [5:0]  ch_len;
    logic        ch_final;
    always_comb begin
        if (state == S_ESC) begin
            ch_val = {12'd0, esc_u};
            ch_len = 6'd20;
            ch_final = esc_final;
        end else if (esc) begin
            ch_val = 32'h0000_FFFF;
            ch_len = 6'd16;
            ch_final = 1'b0;
        end else begin
            ch_val = code;
            ch_len = codelen;
            ch_final = lastblk;
        end
    end
    wire [31:0] va     = ch_val << (6'd32 - ch_len);          // left-align the chunk
    wire [63:0] nb     = bits | ({va, 32'd0} >> cnt);
    wire [6:0]  ncnt   = {1'b0, cnt} + {1'b0, ch_len};
    wire        full   = ncnt[6:5] != 2'b00;                   // >= 32 bits: a word is ready
    wire [5:0]  rest   = full ? ncnt[5:0] - 6'd32 : ncnt[5:0];

    wire out_free = !out_valid || out_ready;
    assign in_ready = !clearing && state == S_IN && out_free;
    assign idle = !clearing && state == S_IN && !out_valid;   // a partial block may wait in `bits`
    wire in_fire = in_valid && in_ready;
    wire step = in_fire || (state == S_ESC && out_free);

    always_ff @(posedge clk)
        if (clearing) acc[cidx] <= '0;
        else if (in_fire) acc[ch] <= a_next;

    always_ff @(posedge clk) begin
        if (rst || clr) begin
            clearing <= 1'b1;
            cidx <= '0;
            state <= S_IN;
            bits <= '0;
            cnt <= '0;
            seq <= '0;
            out_valid <= 1'b0;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            if (out_valid && out_ready) out_valid <= 1'b0;
            if (step) begin
                if (full) begin
                    out_valid <= 1'b1;
                    out_data <= nb[63:32];
                    out_tag <= seq;
                    out_last <= ch_final && rest == 6'd0;
                end
                bits <= full ? {nb[31:0], 32'd0} : nb;
                cnt <= rest;
                if (state == S_IN && esc) begin
                    state <= S_ESC;
                    esc_u <= u;
                    esc_final <= lastblk;
                end else if (ch_final && rest != 6'd0) begin
                    state <= S_FLUSH;
                end else begin
                    state <= S_IN;
                    if (ch_final) seq <= seq + 8'd1;
                end
            end else if (state == S_FLUSH && out_free) begin
                out_valid <= 1'b1;
                out_data <= bits[63:32];
                out_tag <= seq;
                out_last <= 1'b1;
                bits <= '0;
                cnt <= '0;
                seq <= seq + 8'd1;
                state <= S_IN;
            end
        end
    end
endmodule
