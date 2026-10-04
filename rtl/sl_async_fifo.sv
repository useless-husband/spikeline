// Asynchronous FIFO between two unrelated clocks (2^AW entries, AW >= 2).
//
// Each side counts in binary and also keeps the Gray code of its pointer in a
// register.  Only the Gray pointers cross, each through a two-flop
// synchroniser: consecutive Gray values differ in one bit, so even if the
// synchroniser catches a change one edge late, the other side sees either the
// old or the new pointer, never a mix.  Both sides compare their own *next*
// pointer with the other side's synchronised pointer and register the result,
// so `w_ready` (not full) and `r_valid` (not empty) are glitch-free flops.
//
// A synchronised pointer is always a few cycles old, which only makes each
// side conservative: the writer may think the FIFO is full a little longer,
// the reader may think it is empty a little longer.  Neither can overrun.
//
// Pointers carry one extra (wrap) bit.  Empty: Gray pointers equal.  Full:
// the top two Gray bits differ and the rest are equal.
//
// The storage is written in the write domain and read combinationally in the
// read domain.  A slot is only read after its write has been made visible
// through the synchronised write pointer, i.e. at least two read-clock edges
// after the write.
//
// `w_empty` is the write side's (conservative) view of "everything written
// has been read"; the tile uses it to report idle.
module sl_async_fifo #(
    parameter int W  = 41,
    parameter int AW = 3
) (
    input  logic         wclk,
    input  logic         wrst,
    input  logic         w_valid,
    output logic         w_ready,
    input  logic [W-1:0] w_data,
    output logic         w_empty,
    input  logic         rclk,
    input  logic         rrst,
    output logic         r_valid,
    input  logic         r_ready,
    output logic [W-1:0] r_data
);
    logic [W-1:0] mem [0:(1 << AW) - 1];

    // ---------------------------------------------------------------- write
    logic [AW:0] wbin, wgray, rgray_w;
    wire wfire = w_valid && w_ready;
    wire [AW:0] wbin_n = wbin + {{AW{1'b0}}, wfire};
    wire [AW:0] wgray_n = (wbin_n >> 1) ^ wbin_n;
    wire [AW:0] full_val = {~rgray_w[AW:AW-1], rgray_w[AW-2:0]};

    always_ff @(posedge wclk)
        if (wrst) begin
            wbin <= '0;
            wgray <= '0;
            w_ready <= 1'b0;
        end else begin
            wbin <= wbin_n;
            wgray <= wgray_n;
            w_ready <= wgray_n != full_val;
        end

    always_ff @(posedge wclk)
        if (wfire) mem[wbin[AW-1:0]] <= w_data;

    assign w_empty = wgray == rgray_w;

    // ----------------------------------------------------------------- read
    logic [AW:0] rbin, rgray, wgray_r;
    wire rfire = r_valid && r_ready;
    wire [AW:0] rbin_n = rbin + {{AW{1'b0}}, rfire};
    wire [AW:0] rgray_n = (rbin_n >> 1) ^ rbin_n;

    always_ff @(posedge rclk)
        if (rrst) begin
            rbin <= '0;
            rgray <= '0;
            r_valid <= 1'b0;
        end else begin
            rbin <= rbin_n;
            rgray <= rgray_n;
            r_valid <= rgray_n != wgray_r;
        end

    assign r_data = mem[rbin[AW-1:0]];

    // ------------------------------------------------------- synchronisers
    sl_sync2 #(.W(AW + 1)) u_r2w (.clk(wclk), .rst(wrst), .d(rgray), .q(rgray_w));
    sl_sync2 #(.W(AW + 1)) u_w2r (.clk(rclk), .rst(rrst), .d(wgray), .q(wgray_r));
endmodule
