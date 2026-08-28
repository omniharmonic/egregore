#version 300 es
// crt — contemporary phosphor tube. Not the 90s pastiche: the curvature is a
// couple of percent, the scanlines are resolution-aware, and the slot mask is
// barely above the noise floor. What you should notice is that the image has a
// surface, not that a "CRT filter" was applied.
//
//   * Barrel curvature with a soft black surround (2-3%).
//   * Scanlines whose period is chosen for ~400 lines on any display, snapped
//     to whole device pixels, and faded to zero as that period approaches the
//     Nyquist limit — a fixed or fractional period is what makes CRT shaders
//     shimmer on 4K and moire on laptops.
//   * A slot mask of RGB triads at ~6% opacity, sized in device pixels and
//     faded out by the same Nyquist rule.
//   * Horizontal sync wobble that grows with u_low.
//   * Occasional single-line displacement on u_onset, held for a beat.
//   * Gentle phosphor persistence pulled from u_prev.
//
// Feedback safety: persistence uses max-lighten under a luminance gate, the
// same contract feedback.frag establishes. `max` has its fixed point at the
// live image, so a still frame is untouched and the loop gain is <= 1 no
// matter what else is in the stack.
precision highp float;

uniform sampler2D u_tex;
uniform sampler2D u_prev;
uniform vec2  u_res;
uniform float u_time;
uniform float u_low;
uniform float u_onset;
uniform float u_rms;
uniform float u_intensity;

out vec4 fragColor;

float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

float hash21(vec2 p) {
  vec3 p3 = fract(vec3(p.x, p.y, p.x) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;

  float low   = clamp(u_low, 0.0, 1.0);
  float onset = clamp(u_onset, 0.0, 1.0);
  float inten = clamp(u_intensity, 0.0, 1.0);

  // --- geometry: barrel curvature -----------------------------------------
  vec2 c = uv * 2.0 - 1.0;
  float r2 = dot(c, c);
  c *= 1.0 + 0.028 * r2 + 0.010 * r2 * r2;
  vec2 guv = c * 0.5 + 0.5;

  // --- horizontal sync wobble ---------------------------------------------
  // Two frequencies: a slow whole-frame breath and a faster per-line tremor.
  float wob = (0.0009 + 0.0060 * low)
            * (0.62 * sin(guv.y * 41.0 + u_time * 2.10)
             + 0.38 * sin(guv.y *  6.3 - u_time * 0.71));
  guv.x += wob;

  // --- occasional line displacement ---------------------------------------
  // Bands re-roll ~3x a second and hold, so a torn line is legible as an event
  // instead of dissolving into per-frame noise.
  float bands = 96.0;
  float li = floor(guv.y * bands);
  float ep = floor(u_time * 3.1);
  float lh = hash21(vec2(li, ep));
  float sel = step(1.0 - (0.004 + 0.090 * onset * onset), lh);
  float tear = sel * (hash21(vec2(li, ep + 57.0)) - 0.5) * (0.020 + 0.075 * onset);
  guv.x += tear;

  // --- sample, with the tube's black surround -----------------------------
  vec2 g = smoothstep(vec2(-0.004), vec2(0.010), guv) *
           smoothstep(vec2(-0.004), vec2(0.010), 1.0 - guv);
  float inside = g.x * g.y;

  vec3 col = texture(u_tex, clamp(guv, 0.0, 1.0)).rgb * inside;

  // --- phosphor persistence ------------------------------------------------
  // Read the previous *presented* frame at the same curved coordinate, so the
  // persistence follows the tube geometry rather than the flat frame.
  vec3 prev = texture(u_prev, clamp(guv, 0.0, 1.0)).rgb * inside;
  float pl = luma(prev);
  float gate = 1.0 - smoothstep(0.34, 0.76, pl);   // no persistence in highlights
  float persist = (0.40 + 0.20 * inten) * gate;
  col = max(col, prev * persist);

  // --- scanlines -----------------------------------------------------------
  // Aim for ~400 lines regardless of display, but *snap the period to whole
  // device pixels*: a fractional period beats against the pixel grid and the
  // result is a slow moire crawling up the screen, which is the single most
  // common way a CRT shader gives itself away. Below three pixels the pattern
  // is at the Nyquist limit, so the amplitude is faded out instead.
  float period = max(2.0, floor(u_res.y / 400.0 + 0.5));
  float sAmp = 0.115 * smoothstep(2.0, 3.4, period);
  float s = 0.5 + 0.5 * cos(6.28318530718 * gl_FragCoord.y / period);
  col *= 1.0 - sAmp * s;

  // --- slot mask -----------------------------------------------------------
  // Triads sized in device pixels: three phosphor stripes, each at least one
  // pixel wide, faded by the same rule as the scanlines.
  float stripe = max(1.0, floor(u_res.x / 640.0));
  float triad = stripe * 3.0;
  float mAmp = 0.060 * smoothstep(2.4, 4.2, triad);
  float idx = floor(mod(gl_FragCoord.x / stripe, 3.0));
  vec3 mask = vec3(
    1.0 - mAmp * (1.0 - step(abs(idx - 0.0), 0.5)),
    1.0 - mAmp * (1.0 - step(abs(idx - 1.0), 0.5)),
    1.0 - mAmp * (1.0 - step(abs(idx - 2.0), 0.5))
  );
  // Attenuation only — the peak channel is left at 1.0 rather than normalised
  // back up. A mask that multiplies any channel above 1.0 is a gain term, and
  // a gain term inside the u_prev loop is exactly what must not exist here.
  // The cost is a ~4% average dim, which is below the visible threshold.
  col *= mask;

  // --- tube glass ----------------------------------------------------------
  // A faint bloom off the glass at the centre, and the surround falloff.
  float vig = 1.0 - 0.22 * smoothstep(0.35, 1.25, length((uv - 0.5) * 2.0));
  col *= mix(vig, 1.0, 0.25 * clamp(u_rms, 0.0, 1.0));

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
