// Synchronous first-word-fall-through FIFO, 2^AW entries.
// w_ready and r_valid come straight from registers, so the FIFO breaks every
// combinational valid/ready path that passes through it.
module sl_fifo #(
    parameter int W  = 41,
    parameter int AW = 3
) (
    input  logic         clk,
    input  logic         rst,
    input  logic         w_valid,
    output logic         w_ready,
    input  logic [W-1:0] w_data,
    output logic         r_valid,
    input  logic         r_ready,
    output logic [W-1:0] r_data
);
    logic [W-1:0] mem [0:(1 << AW) - 1];
    logic [AW:0] wp, rp;
    wire wfire = w_valid && w_ready;
    wire rfire = r_valid && r_ready;

    always_ff @(posedge clk)
        if (rst) begin
            wp <= '0;
            rp <= '0;
        end else begin
            if (wfire) wp <= wp + 1'b1;
            if (rfire) rp <= rp + 1'b1;
        end

    always_ff @(posedge clk)
        if (wfire) mem[wp[AW-1:0]] <= w_data;

    assign w_ready = !rst && !(wp[AW] != rp[AW] && wp[AW-1:0] == rp[AW-1:0]);
    assign r_valid = wp != rp;
    assign r_data  = mem[rp[AW-1:0]];
endmodule
