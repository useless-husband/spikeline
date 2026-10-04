// Reset synchroniser: asserts asynchronously with `arst`, releases
// synchronously to `clk`, so every flip-flop of the domain leaves reset on
// the same edge.  Logic in the domain then uses `rst` as a synchronous reset.
module sl_rst_sync (
    input  logic clk,
    input  logic arst,
    output logic rst
);
    logic r1;
    always_ff @(posedge clk or posedge arst)
        if (arst) {rst, r1} <= 2'b11;
        else {rst, r1} <= {r1, 1'b0};
endmodule
