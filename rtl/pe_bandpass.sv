// Band-pass filter PE: 300-6000 Hz, two direct-form-I biquads per channel,
// time-shared over all channels with ONE multiplier (24 x 18 bit, the size of
// a DSP48E1).  Bit-exact reference: model/slmodel.py, class Bandpass.
//
// Per sample: 1 accept cycle, 11 multiply/accumulate steps, 1 output cycle
// = 13 cycles, so 96 channels x 30 kHz need a 37.44 MHz clock.  That is the
// trade this PE makes: one multiplier and a fast clock instead of ten
// multipliers and a slow one.
//
// Fixed point: input x is 16-bit; section signals are Q18.6 (STATE_W = 24
// bits); coefficients are Q2.16 (18 bits, rtl/bandpass_coefs.svh).  The
// 48-bit accumulator is exact; each section rounds once (add 2^15, shift
// right 16) and saturates to 24 bits.  Output: round away 6 fractional bits,
// saturate to 16 bits.
//
// Section 1's input history is section 0's output history, so per channel
// only six values are stored: x[n-1], x[n-2], v0[n-1], v0[n-2], v1[n-1],
// v1[n-2] (128 bits, in a 96-entry memory with an asynchronous read port).
//
// Step schedule (preg = product register, acc = accumulator):
//   s0  preg = u0*S0_B0                      (u0 = x << 6)
//   s1  preg = x1*S0_B1      acc  = preg
//   s2  preg = x2*S0_B2      acc += preg
//   s3  preg = y1a*S0_NA1    acc += preg
//   s4  preg = y2a*S0_NA2    acc += preg
//   s5  preg = y1a*S1_B1     acc += preg      -> acc = section 0 sum
//   s6  preg = y2a*S1_B2     v0 = round(acc), acc = preg
//   s7  preg = y1b*S1_NA1    acc += preg
//   s8  preg = y2b*S1_NA2    acc += preg
//   s9  preg = v0*S1_B0      acc += preg
//   s10                      acc += preg      -> acc = section 1 sum
//   out v1 = round(acc), y = round(v1 >> 6)
module pe_bandpass #(
    parameter int NCH = 96
) (
    input  logic        clk,
    input  logic        rst,
    /* verilator lint_off UNUSEDSIGNAL */
    input  logic [31:0] cfg,       // no run-time settings
    input  logic [31:0] in_data,   // only [15:0] is the sample
    /* verilator lint_on UNUSEDSIGNAL */
    input  logic        clr,
    input  logic        in_valid,
    output logic        in_ready,
    input  logic [7:0]  in_tag,
    input  logic        in_last,
    output logic        out_valid,
    input  logic        out_ready,
    output logic [31:0] out_data,
    output logic [7:0]  out_tag,
    output logic        out_last,
    output logic        idle
);
`include "bandpass_coefs.svh"
    localparam int CW = $clog2(NCH);
    localparam int NCH_M1 = NCH - 1;
    localparam logic [CW-1:0] LAST_CH = NCH_M1[CW-1:0];

    // ------------------------------------------------------ channel state
    logic [127:0] st [0:NCH-1];
    logic         mem_we;
    logic [CW-1:0] mem_wa;
    logic [127:0] mem_wd;
    always_ff @(posedge clk)
        if (mem_we) st[mem_wa] <= mem_wd;

    // ----------------------------------------------------------- control
    localparam logic [1:0] S_IN = 2'd0, S_MAC = 2'd1, S_OUT = 2'd2;
    logic [1:0]    state;
    logic [3:0]    step;
    logic          clearing;
    logic [CW-1:0] cidx;

    logic [CW-1:0]       ch;
    logic [7:0]          tag_r;
    logic                last_r;
    logic signed [15:0]  x0, x1, x2;
    logic signed [23:0]  y1a, y2a, y1b, y2b, v0;
    logic signed [41:0]  preg;
    logic signed [47:0]  acc;

    wire in_fire  = in_valid && in_ready;
    wire out_free = !out_valid || out_ready;
    assign in_ready = state == S_IN && !clearing;
    assign idle = state == S_IN && !clearing && !out_valid;

    // round-and-saturate helpers
    function automatic logic signed [23:0] round_state(input logic signed [47:0] a);
        logic signed [47:0] t;
        t = (a + 48'sd32768) >>> 16;
        if (t > 48'sd8388607) round_state = 24'sd8388607;
        else if (t < -48'sd8388608) round_state = -24'sd8388608;
        else round_state = t[23:0];
    endfunction

    function automatic logic signed [15:0] round_out(input logic signed [23:0] v);
        logic signed [23:0] t;
        t = (v + 24'sd32) >>> 6;
        if (t > 24'sd32767) round_out = 16'sd32767;
        else if (t < -24'sd32768) round_out = -16'sd32768;
        else round_out = t[15:0];
    endfunction

    // --------------------------------------------------- multiplier input
    logic signed [23:0] op_a;
    logic signed [17:0] op_b;
    wire signed [23:0] u0 = {{2{x0[15]}}, x0, 6'd0};
    wire signed [23:0] u1 = {{2{x1[15]}}, x1, 6'd0};
    wire signed [23:0] u2 = {{2{x2[15]}}, x2, 6'd0};
    always_comb begin
        op_a = '0;
        op_b = '0;
        case (step)
            4'd0: begin op_a = u0;                      op_b = S0_B0;  end
            4'd1: begin op_a = u1;                      op_b = S0_B1;  end
            4'd2: begin op_a = u2;                      op_b = S0_B2;  end
            4'd3: begin op_a = y1a;                     op_b = S0_NA1; end
            4'd4: begin op_a = y2a;                     op_b = S0_NA2; end
            4'd5: begin op_a = y1a;                     op_b = S1_B1;  end
            4'd6: begin op_a = y2a;                     op_b = S1_B2;  end
            4'd7: begin op_a = y1b;                     op_b = S1_NA1; end
            4'd8: begin op_a = y2b;                     op_b = S1_NA2; end
            4'd9: begin op_a = v0;                      op_b = S1_B0;  end
            default: ;
        endcase
    end
    wire signed [41:0] prod = {{18{op_a[23]}}, op_a} * {{24{op_b[17]}}, op_b};
    wire signed [47:0] preg_x = {{6{preg[41]}}, preg};

    wire signed [23:0] v1 = round_state(acc);
    wire signed [15:0] y  = round_out(v1);
    wire [127:0] rd = st[in_tag[CW-1:0]];

    always_comb begin
        mem_we = 1'b0;
        mem_wa = ch;
        mem_wd = {x0, x1, v0, y1a, v1, y1b};
        if (clearing) begin
            mem_we = 1'b1;
            mem_wa = cidx;
            mem_wd = '0;
        end else if (state == S_OUT && out_free) begin
            mem_we = 1'b1;
        end
    end

    always_ff @(posedge clk) begin
        if (rst || clr) begin
            state <= S_IN;
            step <= '0;
            clearing <= 1'b1;
            cidx <= '0;
            out_valid <= 1'b0;
        end else begin
            if (clearing) begin
                cidx <= cidx + 1'b1;
                if (cidx == LAST_CH) clearing <= 1'b0;
            end
            if (out_valid && out_ready) out_valid <= 1'b0;
            case (state)
                S_IN: if (in_fire) begin
                    ch <= in_tag[CW-1:0];
                    tag_r <= in_tag;
                    last_r <= in_last;
                    x0 <= in_data[15:0];
                    {x1, x2, y1a, y2a, y1b, y2b} <= rd;
                    step <= '0;
                    state <= S_MAC;
                end
                S_MAC: begin
                    preg <= prod;
                    if (step == 4'd1 || step == 4'd6) acc <= preg_x;
                    else if (step != 4'd0) acc <= acc + preg_x;
                    if (step == 4'd6) v0 <= round_state(acc);
                    step <= step + 1'b1;
                    if (step == 4'd10) state <= S_OUT;
                end
                default: if (out_free) begin   // S_OUT
                    out_valid <= 1'b1;
                    out_data <= {{16{y[15]}}, y};
                    out_tag <= tag_r;
                    out_last <= last_r;
                    state <= S_IN;
                end
            endcase
        end
    end
endmodule
