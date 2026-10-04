// One processing element plus its boundary to the fabric.
//
// Fabric side (clk_f): a 41-bit stream in, a 41-bit stream out
// ({last, tag[7:0], data[31:0]}), a configuration write port and an idle flag.
//
// ASYNC = 1 (the PE has its own clock, GALS):
//     fabric --async FIFO--> PE (clk_p) --async FIFO--> fabric
//     configuration crosses through sl_cfg_cdc (toggle handshake)
//     the PE domain gets its own reset synchroniser
// ASYNC = 0 (the PE shares the fabric clock):
//     fabric --sync FIFO--> PE --> fabric
//
// Either way the tile's in_ready and out_valid come from registers, so no
// combinational valid/ready path crosses a tile.  That is what lets the
// crossbar connect tiles in any order, even in a loop, without creating a
// combinational cycle.
//
// Idle (for draining before reconfiguration).  The PE domain reports
// "PE idle, input FIFO read side empty, output FIFO write side empty", which
// crosses through a two-flop synchroniser; the fabric adds its own view of
// both FIFOs.  The two views travel through different synchronisers and may
// arrive one cycle apart, so the fabric only reports idle after the
// combined condition has held for 7 consecutive cycles.
module sl_tile #(
    parameter int          KIND      = 0,      // 0 band-pass 1 noise 2 NEO 3 snippet 4 predictor 5 Rice
    parameter bit          ASYNC     = 1'b0,
    parameter int          NCH       = 96,
    parameter logic [31:0] CFG_RESET = 32'd0,
    parameter int          FIFO_LOG2 = 3
) (
    input  logic        clk_f,
    input  logic        rst_f,
    input  logic        clk_p,
    input  logic        arst,
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [40:0] in_bus,
    output logic        out_valid,
    input  logic        out_ready,
    output logic [40:0] out_bus,
    input  logic        cfg_wr,
    input  logic [31:0] cfg_wdata,
    output logic        cfg_busy,
    output logic        idle
);
    logic        clk, rst;
    logic        p_in_valid, p_in_ready;
    logic [40:0] p_in_bus;
    logic        p_out_valid, p_out_ready, p_out_last;
    logic [31:0] p_out_data;
    logic [7:0]  p_out_tag;
    logic [31:0] cfg;
    logic        clr, pe_idle, idle_raw;

    if (ASYNC) begin : g_async
        logic in_wempty, out_wempty, idle_p, idle_ps;
        assign clk = clk_p;
        sl_rst_sync u_rst (.clk(clk_p), .arst(arst), .rst(rst));
        sl_async_fifo #(.W(41), .AW(FIFO_LOG2)) u_in (
            .wclk(clk_f), .wrst(rst_f), .w_valid(in_valid), .w_ready(in_ready), .w_data(in_bus),
            .w_empty(in_wempty),
            .rclk(clk_p), .rrst(rst), .r_valid(p_in_valid), .r_ready(p_in_ready), .r_data(p_in_bus));
        sl_async_fifo #(.W(41), .AW(FIFO_LOG2)) u_out (
            .wclk(clk_p), .wrst(rst), .w_valid(p_out_valid), .w_ready(p_out_ready),
            .w_data({p_out_last, p_out_tag, p_out_data}), .w_empty(out_wempty),
            .rclk(clk_f), .rrst(rst_f), .r_valid(out_valid), .r_ready(out_ready), .r_data(out_bus));
        sl_cfg_cdc #(.ASYNC(1'b1), .RESET(CFG_RESET)) u_cfg (
            .clk_a(clk_f), .rst_a(rst_f), .wr(cfg_wr), .wdata(cfg_wdata), .busy(cfg_busy),
            .clk_b(clk_p), .rst_b(rst), .pe_idle(pe_idle), .cfg(cfg), .clr(clr));
        always_ff @(posedge clk_p)
            if (rst) idle_p <= 1'b0;
            else idle_p <= pe_idle && !p_in_valid && out_wempty;
        sl_sync2 u_idle (.clk(clk_f), .rst(rst_f), .d(idle_p), .q(idle_ps));
        assign idle_raw = idle_ps && in_wempty && !out_valid && !cfg_busy;
    end else begin : g_sync
        assign clk = clk_f;
        assign rst = rst_f;
        sl_fifo #(.W(41), .AW(FIFO_LOG2)) u_in (
            .clk(clk_f), .rst(rst_f), .w_valid(in_valid), .w_ready(in_ready), .w_data(in_bus),
            .r_valid(p_in_valid), .r_ready(p_in_ready), .r_data(p_in_bus));
        assign out_valid = p_out_valid;
        assign p_out_ready = out_ready;
        assign out_bus = {p_out_last, p_out_tag, p_out_data};
        sl_cfg_cdc #(.ASYNC(1'b0), .RESET(CFG_RESET)) u_cfg (
            .clk_a(clk_f), .rst_a(rst_f), .wr(cfg_wr), .wdata(cfg_wdata), .busy(cfg_busy),
            .clk_b(clk_f), .rst_b(rst_f), .pe_idle(pe_idle), .cfg(cfg), .clr(clr));
        assign idle_raw = pe_idle && !p_in_valid && !cfg_busy;
        wire unused_clk = clk_p ^ arst;   // this tile runs on the fabric clock
    end

    logic [2:0] held;
    always_ff @(posedge clk_f)
        if (rst_f || !idle_raw) held <= '0;
        else if (held != 3'd7) held <= held + 3'd1;
    assign idle = held == 3'd7;

`define SL_PE_PORTS \
        .clk(clk), .rst(rst), .cfg(cfg), .clr(clr), \
        .in_valid(p_in_valid), .in_ready(p_in_ready), .in_data(p_in_bus[31:0]), \
        .in_tag(p_in_bus[39:32]), .in_last(p_in_bus[40]), \
        .out_valid(p_out_valid), .out_ready(p_out_ready), .out_data(p_out_data), \
        .out_tag(p_out_tag), .out_last(p_out_last), .idle(pe_idle)

    if (KIND == 0) begin : g_pe
        pe_bandpass #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end else if (KIND == 1) begin : g_pe
        pe_noise #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end else if (KIND == 2) begin : g_pe
        pe_neo #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end else if (KIND == 3) begin : g_pe
        pe_snippet #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end else if (KIND == 4) begin : g_pe
        pe_predict #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end else begin : g_pe
        pe_rice #(.NCH(NCH)) u_pe (`SL_PE_PORTS);
    end
`undef SL_PE_PORTS
endmodule
