// spikeline: neural-signal processing fabric for 96 channels x 30 kHz x 16 bit.
//
//   in port --> [ crossbar ] <--> 6 PE tiles (each optionally in its own clock domain)
//                   |
//                   +--> 3 out ports
//
// Crossbar sources:      0 in port, 1 band-pass, 2 noise, 3 NEO, 4 snippet,
//                        5 predictor, 6 Rice
// Crossbar destinations: 0 band-pass, 1 noise, 2 NEO, 3 snippet, 4 predictor,
//                        5 Rice, 6 out0, 7 out1, 8 out2
//
// Register map (32-bit words, bus in the fabric clock domain):
//   0x00 ID       "SPKL" (0x53504B4C)
//   0x01 PARAMS   {NCH[7:0], sources[7:0], destinations[7:0], 2'b0, ASYNC_MASK[5:0]}
//   0x02 STATUS   [5:0] tile idle, [6] in port empty, [9:7] out ports empty,
//                 [15:10] PE config busy, [16] drained (all of the above idle/empty)
//   0x10+d ROUTE[d]  source for destination d (0..8); 15 = not connected
//   0x20+p PE_CFG[p] configuration word of PE p (0..5); bit 31 = clear the PE's
//                    state (self-clearing); a write waits while the previous one
//                    is still crossing into the PE's clock domain
//   0x30   IN_WORDS        words accepted at the in port
//   0x31+k OUT_WORDS[k]    words delivered at out port k
// Routes and PE state may only be changed while STATUS[16] is set.
//
// Stream words are {last, tag[7:0], data[31:0]}; see docs/report.md.
module spikeline_top #(
    parameter int          NCH        = 96,
    parameter logic [5:0]  ASYNC_MASK = 6'b111111,
    parameter int          FIFO_LOG2  = 3,
    parameter int          PORT_LOG2  = 4,
    parameter logic [31:0] NOISE_CFG  = 32'h0004_0010,   // step 16, warm-up 1024 frames
    parameter logic [31:0] NEO_CFG    = 32'h001E_0960,   // C 2400, refractory 30, energy mode
    parameter logic [31:0] PRED_CFG   = 32'h0000_001D    // order 1, blocks of 2^7 frames
) (
    input  logic        clk_fab,
    input  logic        clk_bp,
    input  logic        clk_noise,
    input  logic        clk_neo,
    input  logic        clk_snip,
    input  logic        clk_pred,
    input  logic        clk_rice,
    input  logic        arst,
    // register bus
    input  logic        cfg_valid,
    output logic        cfg_ready,
    input  logic        cfg_we,
    input  logic [7:0]  cfg_addr,
    input  logic [31:0] cfg_wdata,
    output logic [31:0] cfg_rdata,
    // input stream (ADC samples)
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [31:0] in_data,
    input  logic [7:0]  in_tag,
    input  logic        in_last,
    // three output streams
    output logic [2:0]  out_valid,
    input  logic [2:0]  out_ready,
    output logic [95:0] out_data,
    output logic [23:0] out_tag,
    output logic [2:0]  out_last
);
    localparam int NS = 7;
    localparam int ND = 9;
    localparam int W = 41;

    logic rst;
    sl_rst_sync u_rst (.clk(clk_fab), .arst(arst), .rst(rst));

    // ------------------------------------------------------------ crossbar
    logic [NS-1:0]   src_valid, src_ready;
    logic [NS*W-1:0] src_bus;
    logic [ND-1:0]   dst_valid, dst_ready;
    logic [ND*W-1:0] dst_bus;
    logic [ND*4-1:0] sel;
    logic            sel_wr;

    sl_xbar #(.NS(NS), .ND(ND), .W(W)) u_xbar (
        .clk(clk_fab), .rst(rst), .src_valid(src_valid), .src_ready(src_ready), .src_bus(src_bus),
        .dst_valid(dst_valid), .dst_ready(dst_ready), .dst_bus(dst_bus), .sel(sel), .sel_wr(sel_wr));

    // ------------------------------------------------------------ in port
    logic in_empty;
    sl_fifo #(.W(W), .AW(PORT_LOG2)) u_in (
        .clk(clk_fab), .rst(rst), .w_valid(in_valid), .w_ready(in_ready),
        .w_data({in_last, in_tag, in_data}),
        .r_valid(src_valid[0]), .r_ready(src_ready[0]), .r_data(src_bus[0 +: W]));
    assign in_empty = !src_valid[0];

    // ------------------------------------------------------------ out ports
    logic [2:0] out_empty;
    for (genvar k = 0; k < 3; k++) begin : g_out
        logic [W-1:0] bus;
        sl_fifo #(.W(W), .AW(PORT_LOG2)) u_out (
            .clk(clk_fab), .rst(rst), .w_valid(dst_valid[6+k]), .w_ready(dst_ready[6+k]),
            .w_data(dst_bus[(6+k)*W +: W]),
            .r_valid(out_valid[k]), .r_ready(out_ready[k]), .r_data(bus));
        assign out_data[32*k +: 32] = bus[31:0];
        assign out_tag[8*k +: 8] = bus[39:32];
        assign out_last[k] = bus[40];
        assign out_empty[k] = !out_valid[k];
    end

    // ------------------------------------------------------------ PE tiles
    logic [5:0]  tile_idle, cfg_busy, cfg_wr;
    logic [5:0]  pe_clk;
    assign pe_clk = {clk_rice, clk_pred, clk_snip, clk_neo, clk_noise, clk_bp};

    for (genvar p = 0; p < 6; p++) begin : g_tile
        localparam logic [31:0] RESET_CFG = p == 1 ? NOISE_CFG : p == 2 ? NEO_CFG : p == 4 ? PRED_CFG : 32'd0;
        sl_tile #(.KIND(p), .ASYNC(ASYNC_MASK[p]), .NCH(NCH), .CFG_RESET(RESET_CFG), .FIFO_LOG2(FIFO_LOG2)) u_tile (
            .clk_f(clk_fab), .rst_f(rst), .clk_p(pe_clk[p]), .arst(arst),
            .in_valid(dst_valid[p]), .in_ready(dst_ready[p]), .in_bus(dst_bus[p*W +: W]),
            .out_valid(src_valid[p+1]), .out_ready(src_ready[p+1]), .out_bus(src_bus[(p+1)*W +: W]),
            .cfg_wr(cfg_wr[p]), .cfg_wdata(cfg_wdata), .cfg_busy(cfg_busy[p]), .idle(tile_idle[p]));
    end

    // ------------------------------------------------------------ registers
    logic [31:0] pe_cfg [0:5];
    logic [31:0] in_words;
    logic [95:0] out_words;          // three 32-bit counters
    wire         drained = &tile_idle && in_empty && &out_empty && cfg_busy == 6'd0;
    wire [31:0]  status = {15'd0, drained, cfg_busy, out_empty, in_empty, tile_idle};

    wire wr       = cfg_valid && cfg_we;
    wire [3:0] ahi = cfg_addr[7:4];
    wire [3:0] alo = cfg_addr[3:0];
    wire is_route = ahi == 4'h1 && alo < 4'd9;
    wire is_pecfg = ahi == 4'h2 && alo < 4'd6;
    wire [2:0] pidx = alo[2:0];
    assign cfg_ready = !(wr && is_pecfg && cfg_busy[pidx]);
    assign sel_wr = wr && is_route;
    for (genvar p = 0; p < 6; p++) begin : g_cfgwr
        localparam int PI = p;
        assign cfg_wr[p] = wr && is_pecfg && pidx == PI[2:0] && !cfg_busy[p];
    end

    always_ff @(posedge clk_fab)
        if (rst) begin
            sel <= {ND{4'hF}};
            pe_cfg[0] <= 32'd0;
            pe_cfg[1] <= NOISE_CFG;
            pe_cfg[2] <= NEO_CFG;
            pe_cfg[3] <= 32'd0;
            pe_cfg[4] <= PRED_CFG;
            pe_cfg[5] <= 32'd0;
            in_words <= '0;
            out_words <= '0;
        end else begin
            if (sel_wr) sel[4*alo +: 4] <= cfg_wdata[3:0];
            if (wr && is_pecfg && !cfg_busy[pidx]) pe_cfg[pidx] <= {1'b0, cfg_wdata[30:0]};
            if (in_valid && in_ready) in_words <= in_words + 32'd1;
            for (int k = 0; k < 3; k++)
                if (out_valid[k] && out_ready[k]) out_words[32*k +: 32] <= out_words[32*k +: 32] + 32'd1;
        end

    wire [31:0] out_w0 = out_words[31:0];
    wire [31:0] out_w1 = out_words[63:32];
    wire [31:0] out_w2 = out_words[95:64];
    always_comb begin
        cfg_rdata = 32'd0;
        if (is_route) cfg_rdata = {28'd0, sel[4*alo +: 4]};
        else if (is_pecfg) cfg_rdata = pe_cfg[pidx];
        else case (cfg_addr)
            8'h00: cfg_rdata = 32'h5350_4B4C;
            8'h01: cfg_rdata = {NCH[7:0], 8'd7, 8'd9, 2'd0, ASYNC_MASK};
            8'h02: cfg_rdata = status;
            8'h30: cfg_rdata = in_words;
            8'h31: cfg_rdata = out_w0;
            8'h32: cfg_rdata = out_w1;
            8'h33: cfg_rdata = out_w2;
            default: cfg_rdata = 32'd0;
        endcase
    end
endmodule
