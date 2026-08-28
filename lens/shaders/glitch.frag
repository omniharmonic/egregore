#version 300 es
// glitch — block displacement with in-block RGB shear.
//
// The image is partitioned into irregular blocks (variable row heights, a
// per-row column count) and a *sparse* subset of them slips sideways. Two
// things keep this from reading as noise:
//
//   1. Blocks hold. Each block runs on its own slow clock (0.26-0.95 s), so a
//      slip is a decision that persists for a beat instead of a per-frame
//      flicker. Re-rolling every frame is what makes cheap glitch shaders look
//      like TV static; holding is what makes them look like a codec failing.
//   2. Displacement is derived from the picture. The magnitude scales with the
//      local horizontal luminance gradient and the direction is biased down
//      that gradient, so blocks tear along edges in the frame rather than at
//      random — the slip lands on content, not on flat sky.
//
// Amount is gated by u_onset (which arrives already smoothed, fast attack /
// ~1.2 s release, so squaring it gives a burst that settles on its own) over a
// baseline from u_intensity. A quiet room is a nearly clean image with the
// occasional single-block slip.
//
// Feedback safety: the output is a resample of u_tex plus an attenuation, so
// no texel can exceed the brightest texel this pass read. The single additive
// term (the seam on a slip's leading edge) is gated to the complement of the
// highlights, so it cannot lift a bright frame further on a subsequent trip
// through the u_prev loop. Loop gain stays <= 1 whatever else is in the stack.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_onset;
uniform float u_intensity;
uniform float u_energy;
uniform float u_mid;

out vec4 fragColor;

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

float hash11(float n) {
  n = fract(n * 0.1031);
  n *= n + 33.33;
  n *= n + n;
  return fract(n);
}

float hash21(vec2 p) {
  vec3 p3 = fract(vec3(p.x, p.y, p.x) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float hash31(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.yzx + 33.33);
  return fract((p.x + p.y) * p.z);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;

  float onset = clamp(u_onset, 0.0, 1.0);
  float inten = clamp(u_intensity, 0.0, 1.0);
  float burst = onset * onset;                 // transients read as hits

  // --- irregular partition ------------------------------------------------
  // Coarse rows, roughly half of which are split in two. Cheap, and it breaks
  // the regular banding that a plain uniform grid gives away as a grid.
  const float ROWS = 13.0;
  float ry = uv.y * ROWS;
  float r0 = floor(ry);
  float split = step(0.52, hash11(r0 * 7.13 + 3.0));
  float half_ = floor(fract(ry) * 2.0);
  float rowId = r0 * 2.0 + split * half_;      // integer row identity

  // Column count varies per row, so blocks are not all the same width.
  float cols = floor(mix(3.0, 8.0, hash11(rowId * 11.71 + 1.3)));
  float cx = uv.x * cols;
  vec2 bid = vec2(floor(cx), rowId);

  // --- per-block clock ----------------------------------------------------
  // 1.05-3.8 Hz -> a block's decision holds for 0.26-0.95 s. The phase offset
  // means blocks do not all re-roll on the same tick.
  float rate  = mix(1.05, 3.80, hash21(bid + 17.3));
  float epoch = floor(u_time * rate + hash21(bid + 3.71) * 10.0);
  float h     = hash31(vec3(bid, epoch));

  // Sparse selection. Quiet: ~1-2 of ~70 blocks. On a transient: up to a third
  // of them, then it settles back as the release envelope on u_onset decays.
  // The ceiling is the point — a hit should read as the frame being struck,
  // not as the frame dissolving, so the clean majority never goes away.
  float density = clamp(0.012 + 0.055 * inten + 0.45 * burst, 0.0, 0.30);
  float on = step(1.0 - density, h);

  // --- displacement from image content ------------------------------------
  // Local horizontal luminance gradient at block scale. Blocks slip further
  // where the frame has structure to tear, and barely at all across flats.
  float span = 0.5 / cols;
  vec3 cL = texture(u_tex, vec2(clamp(uv.x - span, 0.0, 1.0), uv.y)).rgb;
  vec3 cR = texture(u_tex, vec2(clamp(uv.x + span, 0.0, 1.0), uv.y)).rgb;
  float grad = luma(cR) - luma(cL);
  float dirBias = grad / (abs(grad) + 0.06);   // soft sign, dead near flat

  float amp = 0.010 + 0.070 * burst + 0.018 * inten
            + 0.010 * clamp(u_energy, 0.0, 1.0);
  float signed = hash31(vec3(bid, epoch + 41.0)) * 2.0 - 1.0;
  float dx = amp * (0.30 + 1.25 * abs(grad)) * (0.55 * signed + 0.45 * dirBias);

  // A rare vertical hold: the block samples a neighbouring row instead of its
  // own. One in ~seven slips, so it stays an event rather than a texture.
  float rowSwap = step(0.86, hash31(vec3(bid, epoch + 97.0)));
  float dy = rowSwap * (hash31(vec3(bid, epoch + 131.0)) - 0.5) * (1.6 / ROWS);

  vec2 off = vec2(dx, dy) * on;

  // --- RGB shear inside the displaced block -------------------------------
  // The shear ramps across the block's width, so the channels skew apart the
  // way a misaligned scanline copy does rather than sitting at a fixed offset.
  float fx = fract(cx);
  float shear = dx * (0.16 + 0.42 * fx) * on * (0.7 + 0.6 * clamp(u_mid, 0.0, 1.0));

  vec3 col;
  col.r = texture(u_tex, mirr(uv + off + vec2(shear, 0.0))).r;
  col.g = texture(u_tex, mirr(uv + off)).g;
  col.b = texture(u_tex, mirr(uv + off - vec2(shear, 0.0))).b;

  // Slipped blocks lose a little level, as a dropped macroblock does. Small:
  // the point is the displacement, not a flashing rectangle.
  col *= 1.0 - 0.10 * on;

  // Hairline seam on the leading edge of a slip — reads as the block boundary
  // without drawing an outline around every block. This is the pass's only
  // additive term, so it is both tiny and gated to the highlights' complement:
  // it cannot lift a bright frame further on a subsequent trip through the
  // u_prev loop, which is what keeps this shader safe after feedback/bloom.
  float seam = on * smoothstep(0.045, 0.0, min(fx, 1.0 - fx)) * abs(dx) * 3.0;
  float room = 1.0 - smoothstep(0.55, 0.90, luma(col));
  col += vec3(0.055, 0.060, 0.052) * clamp(seam, 0.0, 1.0) * room;

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
