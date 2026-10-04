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
    parameter int NS = 7,          // at most 7
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

    // destination side: one 8:1 multiplexer per destination, selected by the
    // low three bits of its route register (NS <= 7, so index 7 and every
    // "not connected" value select nothing valid; the data is then don't-care)
    logic [8*W-1:0] src_pad;
    logic [15:0]    valid_pad, fire_pad;
    assign src_pad   = {{((8 - NS) * W){1'b0}}, src_bus};
    assign valid_pad = {{(16 - NS){1'b0}}, src_valid};
    assign fire_pad  = {{(16 - NS){1'b0}}, src_fire};
    for (genvar d = 0; d < ND; d++) begin : g_mux
        wire [3:0] s = sel[4*d +: 4];
        assign dst_bus[d*W +: W] = src_pad[s[2:0]*W +: W];
        assign dst_valid[d] = valid_pad[s] && !done[d];
        assign mine_fired[d] = fire_pad[s];
    end

    always_ff @(posedge clk)
        for (int d = 0; d < ND; d++)
            if (rst || sel_wr || mine_fired[d]) done[d] <= 1'b0;
            else if (dst_valid[d] && dst_ready[d]) done[d] <= 1'b1;
endmodule
