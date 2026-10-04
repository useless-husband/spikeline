// Moves a 32-bit configuration word from the fabric clock (a) to a PE's
// clock (b).  Bit 31 of the word is a "clear" command: it becomes a one-cycle
// pulse in domain b and is not stored.
//
// ASYNC = 1: toggle handshake.  Side a loads `hold` and flips `req` on the
// same edge, then reports busy until `ack` comes back.  Side b sees `req`
// through a two-flop synchroniser; when it differs from the last value seen,
// b copies `hold` (stable since at least two b-cycles, and unchanged until
// the handshake completes) and flips `ack`.  Only the single-bit req/ack
// toggles are synchronised; the 32 data bits never change while b samples
// them.  The register block stalls bus writes while `busy` is high.
//
// ASYNC = 0: same clock on both sides, a plain register.
module sl_cfg_cdc #(
    parameter bit          ASYNC = 1'b1,
    parameter logic [31:0] RESET = 32'd0
) (
    input  logic        clk_a,
    input  logic        rst_a,
    input  logic        wr,
    input  logic [31:0] wdata,
    output logic        busy,
    input  logic        clk_b,
    input  logic        rst_b,
    output logic [31:0] cfg,
    output logic        clr
);
    if (ASYNC) begin : g_async
        logic [31:0] hold;
        logic req, ack, ack_a, req_b, seen;
        always_ff @(posedge clk_a)
            if (rst_a) begin
                hold <= RESET;
                req <= 1'b0;
            end else if (wr && !busy) begin
                hold <= wdata;
                req <= ~req;
            end
        assign busy = req != ack_a;
        sl_sync2 u_req (.clk(clk_b), .rst(rst_b), .d(req), .q(req_b));
        sl_sync2 u_ack (.clk(clk_a), .rst(rst_a), .d(ack), .q(ack_a));
        always_ff @(posedge clk_b)
            if (rst_b) begin
                seen <= 1'b0;
                ack <= 1'b0;
                cfg <= {1'b0, RESET[30:0]};
                clr <= 1'b0;
            end else begin
                clr <= 1'b0;
                if (req_b != seen) begin
                    seen <= req_b;
                    ack <= req_b;
                    cfg <= {1'b0, hold[30:0]};
                    clr <= hold[31];
                end
            end
    end else begin : g_sync
        always_ff @(posedge clk_a)
            if (rst_a) begin
                cfg <= {1'b0, RESET[30:0]};
                clr <= 1'b0;
            end else begin
                clr <= wr && wdata[31];
                if (wr) cfg <= {1'b0, wdata[30:0]};
            end
        assign busy = 1'b0;
        // domain b is domain a here
        wire unused_b = clk_b ^ rst_b;
    end
endmodule
