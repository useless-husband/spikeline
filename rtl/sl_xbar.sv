// Configurable stream crossbar with multicast.
//
// Each destination d has a route register sel[d] naming one source (a value
// >= NS means "not connected").  Several destinations may name the same
// source; the source's word is then copied to all of them (an eager fork):
// each destination takes the word as soon as it is ready and remembers that
// in done[d]; the source moves on only when every destination has it.  So a
// slow destination delays its siblings but no word is lost or duplicated.
//
// A source that no destination names is stalled (ready = 0), never dropped.
// Rewriting a route clears all done bits, so routes may only be changed
// while the fabric is drained (see STATUS in spikeline_top).
module sl_xbar #(
    parameter int NS = 7,
    parameter int ND = 9,
    parameter int W  = 41
) (
    input  logic            clk,
    input  logic            rst,
    input  logic [NS-1:0]   src_valid,
    output logic [NS-1:0]   src_ready,
    input  logic [NS*W-1:0] src_bus,
    output logic [ND-1:0]   dst_valid,
    input  logic [ND-1:0]   dst_ready,
    output logic [ND*W-1:0] dst_bus,
    input  logic [ND*4-1:0] sel,
    input  logic            sel_wr
);
    logic [ND*NS-1:0] hit;          // hit[d*NS+s]: destination d listens to source s
    logic [ND-1:0]    done;
    logic [NS-1:0]    src_fire;
    logic [ND-1:0]    mine_fired;

    for (genvar d = 0; d < ND; d++) begin : g_dst
        for (genvar s = 0; s < NS; s++) begin : g_src
            localparam int SI = s;
            localparam logic [3:0] SIDX = SI[3:0];
            assign hit[d*NS+s] = sel[4*d +: 4] == SIDX;
        end
    end

    // source side: ready when every listening destination has the word or can take it
    for (genvar s = 0; s < NS; s++) begin : g_col
        logic [ND-1:0] listens, ok;
        for (genvar d = 0; d < ND; d++) begin : g_row
            assign listens[d] = hit[d*NS+s];
            assign ok[d] = !hit[d*NS+s] || done[d] || dst_ready[d];
        end
        assign src_ready[s] = |listens && &ok;
        assign src_fire[s] = src_valid[s] && src_ready[s];
    end

    // destination side: a one-hot multiplexer per destination
    always_comb
        for (int d = 0; d < ND; d++) begin
            dst_valid[d] = 1'b0;
            dst_bus[d*W +: W] = '0;
            mine_fired[d] = 1'b0;
            for (int s = 0; s < NS; s++)
                if (hit[d*NS+s]) begin
                    dst_valid[d] = src_valid[s] && !done[d];
                    dst_bus[d*W +: W] = src_bus[s*W +: W];
                    mine_fired[d] = src_fire[s];
                end
        end

    always_ff @(posedge clk)
        for (int d = 0; d < ND; d++)
            if (rst || sel_wr || mine_fired[d]) done[d] <= 1'b0;
            else if (dst_valid[d] && dst_ready[d]) done[d] <= 1'b1;
endmodule
