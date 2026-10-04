// Moves a 32-bit configuration word from the fabric clock (a) to a PE's
// clock (b).  Bit 31 of the word is a "clear" command: it becomes a one-cycle
// pulse in domain b and is not stored.
//
// A write counts as done only when the PE has applied it: for a clear, when
// the PE has gone busy with its state-clearing sweep and become idle again.
// Until then `busy` stays high, so the fabric's STATUS.drained cannot report
// a drained chip while a clear is still about to start.  (An earlier version
// acknowledged on capture; the idle flag then crossed through a different
// synchroniser and could arrive after the ack -- the real-time bench found
// it as a burst of late samples right after a clear.)
//
// ASYNC = 1: toggle handshake.  Side a loads `hold` and flips `req` on the
// same edge, then reports busy until `ack` comes back.  Side b sees `req`
// through a two-flop synchroniser; when it differs from the last value seen,
// b copies `hold` (stable since at least two b-cycles, and unchanged until
// the handshake completes) and, once the word is applied, flips `ack`.
// Only the single-bit req/ack toggles are synchronised; the 32 data bits
// never change while b samples them.  The register block stalls bus writes
// while `busy` is high.
//
// ASYNC = 0: same clock on both sides; busy covers the clear sweep only.
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
    input  logic        pe_idle,   // domain b: the PE is idle (not clearing)
    output logic [31:0] cfg,
    output logic        clr
);
    // domain b: apply a captured word; `applied` pulses when it is done
    logic        take, applied, pend, started;
    logic [31:0] word;
    always_ff @(posedge clk_b)
        if (rst_b) begin
            cfg <= {1'b0, RESET[30:0]};
            clr <= 1'b0;
            pend <= 1'b0;
            started <= 1'b0;
        end else begin
            clr <= 1'b0;
            if (take) begin
                cfg <= {1'b0, word[30:0]};
                clr <= word[31];
                pend <= word[31];
                started <= 1'b0;
            end else if (pend) begin
                if (!pe_idle) started <= 1'b1;          // the clear sweep has begun
                else if (started) pend <= 1'b0;         // ... and finished
            end
        end
    assign applied = (take && !word[31]) || (pend && pe_idle && started);

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
        assign take = req_b != seen;
        assign word = hold;
        always_ff @(posedge clk_b)
            if (rst_b) begin
                seen <= 1'b0;
                ack <= 1'b0;
            end else begin
                if (take) seen <= req_b;
                if (applied) ack <= seen ^ take;    // the req value just taken or being finished
            end
    end else begin : g_sync
        assign take = wr;
        assign word = wdata;
        assign busy = pend;
        wire unused_sync = clk_a ^ rst_a ^ applied;   // domain b is domain a here
    end
endmodule
