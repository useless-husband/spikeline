// Two-flop synchroniser for signals entering clock domain `clk`.
//
// Only Gray-coded counters, single toggles or quasi-static bits may pass
// through it: each bit is synchronised on its own, so a multi-bit value is
// safe only if at most one bit changes between two samples.
//
// With SL_CDC_JITTER defined (simulation only), a bit that has just changed
// is captured either on this edge or one edge later, chosen at random.  That
// is what metastability can do in silicon (the first flop may resolve to the
// old value), so the tests see every legal arrival order of the bits.
module sl_sync2 #(
    parameter int W = 1
) (
    input  logic         clk,
    input  logic         rst,
    input  logic [W-1:0] d,
    output logic [W-1:0] q
);
    logic [W-1:0] s1;
`ifdef SL_CDC_JITTER
    logic [W-1:0] d_prev;
    logic [W-1:0] late;
    always_ff @(posedge clk) begin
        for (int i = 0; i < W; i++) late[i] = $random % 2 != 0;
        d_prev <= d;
        if (rst) s1 <= '0;
        else s1 <= (d & ~((d ^ d_prev) & late)) | (s1 & ((d ^ d_prev) & late));
    end
`else
    always_ff @(posedge clk)
        if (rst) s1 <= '0;
        else s1 <= d;
`endif
    always_ff @(posedge clk)
        if (rst) q <= '0;
        else q <= s1;
endmodule
