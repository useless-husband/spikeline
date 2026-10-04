// Snippet PE: cuts a short waveform around every detected spike, aligned to
// its peak.  Bit-exact reference: model/slmodel.py, class Snippet.
//
// Input: the NEO PE's stream {det, x}.  Every sample is written into a
// per-channel ring buffer of 2^RL samples (block RAM, address {channel,
// frame mod 2^RL}).  A detection starts a capture on that channel: for ALIGN
// samples (the detection sample and the next ALIGN-1) the largest |x| is
// tracked; once the sample PRE + 1 + POST - 1 = POST after the peak has
// arrived, the window [peak - PRE, peak + POST] is all in the ring and is
// sent as one packet:
//     header: data = frame number of the peak (counted at this PE's input)
//     SNIP_LEN words: sign-extended samples, `last` on the final one
// tag = channel on every word.  Input is stalled while a packet goes out
// (SNIP_LEN + 1 cycles); otherwise one sample per cycle.
//
// Detections are ignored while the channel is already capturing, and in
// frames < PRE (so the window never reaches before the last clear).
//
// The ring is read through a synchronous port that is fed the *next* read
// address, so the data for the current address is always ready and a
// packet streams out at one word per cycle without bubbles.
module pe_snippet #(
    parameter int NCH   = 96,
    parameter int PRE   = 10,
    parameter int POST  = 21,
    parameter int ALIGN = 10,
    parameter int RL    = 5
) (
    input  logic        clk,
    input  logic        rst,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0] cfg,       // no run-time settings
    input  logic [31:0] in_data,   // [16] det, [15:0] sample
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
    localparam int LEN = PRE + 1 + POST;
    localparam int LEN_M1 = LEN - 1;
    localparam int ALIGN_M1 = ALIGN - 1;
    localparam logic [RL-1:0] BACK   = LEN_M1[RL-1:0];
    localparam logic [5:0]    POST6  = POST[5:0];
    localparam logic [5:0]    LAST6  = LEN_M1[5:0];
    localparam logic [4:0]    ALIGN5 = ALIGN_M1[4:0];

    // per channel: active, search left (5 bits), post left (6 bits), peak |x| (17 bits)
    logic [28:0]   st [0:NCH-1];
    logic [15:0]   ring [0:(1 << (CW + RL)) - 1];
    logic [31:0]   fcnt;
    logic          clearing;
    logic [CW-1:0] cidx;

    localparam logic [1:0] S_IN = 2'd0, S_HDR = 2'd1, S_DAT = 2'd2;
    logic [1:0]          state;
    logic [7:0]          e_tag;
    logic [31:0]         e_frame;
    logic [5:0]          idx;
    logic [CW+RL-1:0]    raddr, raddr_n;
    logic [15:0]         rdata;

    wire [CW-1:0] ch = in_tag[CW-1:0];
    wire [15:0]  x   = in_data[15:0];
    wire         det = in_data[16];
    wire [16:0]  ax  = x[15] ? 17'd0 - {1'b1, x} : {1'b0, x};

    wire [28:0] s    = st[ch];
    wire        act  = s[28];
    wire [4:0]  srch = s[27:23];
    wire [5:0]  post = s[22:17];
    wire [16:0] peak = s[16:0];

    wire start     = !act && det && fcnt >= PRE;
    wire searching = act && srch != 5'd0;
    wire better    = searching && ax > peak;
    wire [16:0] peak_n = start || better ? ax : peak;
    wire [5:0]  post_n = start || better ? POST6 : (act ? post - 6'd1 : post);
    wire [4:0]  srch_n = start ? ALIGN5 : (searching ? srch - 5'd1 : srch);
    wire        act_n  = act || start;
    wire        emit   = act_n && post_n == 6'd0 && srch_n == 5'd0;

    wire in_fire  = in_valid && in_ready;
    wire out_fire = out_valid && out_ready;
    assign in_ready  = !clearing && state == S_IN;
    assign out_valid = state != S_IN;
    assign out_data  = state == S_HDR ? e_frame : {{16{rdata[15]}}, rdata};
    assign out_tag   = e_tag;
    assign out_last  = state == S_DAT && idx == LAST6;
    assign idle      = !clearing && state == S_IN;

    // per-channel state
    always_ff @(posedge clk)
        if (clearing) st[cidx] <= '0;
        else if (in_fire) st[ch] <= emit ? 29'd0 : {act_n, srch_n, post_n, peak_n};

    // ring buffer: one write port, one synchronous read port
    wire [RL-1:0] wslot = fcnt[RL-1:0];
    always_ff @(posedge clk) begin
        if (in_fire) ring[{ch, wslot}] <= x;
        rdata <= ring[raddr_n];
    end

    wire [CW+RL-1:0] raddr_inc = {raddr[CW+RL-1:RL], raddr[RL-1:0] + 1'b1};   // wraps inside the ring
    always_comb begin
        raddr_n = raddr;
        if (in_fire && emit)
            raddr_n = {ch, wslot - BACK};       // oldest sample of the window
        else if (state == S_DAT && out_fire)
            raddr_n = raddr_inc;
    end

    always_ff @(posedge clk) begin
        raddr <= raddr_n;
        if (rst || clr) begin
            clearing <= 1'b1;
            cidx <= '0;
            fcnt <= '0;
            state <= S_IN;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            case (state)
                S_IN: if (in_fire) begin
                    if (in_last) fcnt <= fcnt + 1'b1;
                    if (emit) begin
                        state <= S_HDR;
                        e_tag <= in_tag;
                        e_frame <= fcnt - POST;
                    end
                end
                S_HDR: if (out_fire) begin
                    state <= S_DAT;
                    idx <= '0;
                end
                default: if (out_fire) begin   // S_DAT
                    idx <= idx + 1'b1;
                    if (idx == LAST6) state <= S_IN;
                end
            endcase
        end
    end
endmodule
