#version 300 es
// pixelsort — shader-approximated threshold-interval pixel sorting.
//
// A real pixel sort collects a contiguous run of pixels whose luminance falls
// inside a threshold interval, orders them, and writes them back. That is a
// scan, which a fragment shader cannot do. What it *can* do is the visible
// consequence: inside such a run, each pixel ends up holding the brightest
// value the run reached ahead of it. So every fragment marches N samples along
// the sort axis, walking only while the samples stay inside the interval, and
// accumulates toward the brightest one it meets. The result is the familiar
// directional smear that stops dead at the edges of the interval.
//
//   * The interval's lower bound is bound to u_high — bright, airy sound sorts
//     more of the frame — and both edges are softened so runs fade in and out
//     instead of snapping on at a luminance contour.
//   * The axis drifts on two slow sines, so the smear direction never settles
//     and never sweeps fast enough to strobe.
//   * Smear length follows u_rms.
//
// Feedback safety: a fragment's result is bounded above by the brightest
// sample *inside the interval*, and the march terminates the moment luminance
// leaves it. Anything above the interval's top is therefore never lifted, and
// the interval's top is a fixed point of the whole loop — this pass cannot
// climb a stack toward white however many times it is re-fed through u_prev.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_rms;
uniform float u_high;
uniform float u_mid;
uniform float u_energy;
uniform float u_intensity;

out vec4 fragColor;

const int STEPS = 16;

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  vec3 src = texture(u_tex, uv).rgb;
  float l0 = luma(src);

  // --- sort axis ----------------------------------------------------------
  // Near-vertical at rest (the direction gravity-fed sorts read best in),
  // wandering by about +/- 50 degrees over a couple of minutes.
  float ang = 1.5708
            + 0.55 * sin(u_time * 0.0310 + 0.7)
            + 0.32 * sin(u_time * 0.0173 + 2.4)
            + 0.45 * (clamp(u_intensity, 0.0, 1.0) - 0.5);
  vec2 axis = vec2(cos(ang), sin(ang)) * vec2(1.0 / ar, 1.0);

  // --- threshold interval -------------------------------------------------
  float hi = clamp(u_high, 0.0, 1.0);
  float lo_ = mix(0.58, 0.24, hi);
  float width = 0.17 + 0.17 * clamp(u_mid, 0.0, 1.0);
  float top = lo_ + width;
  float soft = 0.055;

  // How strongly a given luminance belongs to the interval. Used both as the
  // mask for the fragment itself and as the "run is still alive" term.
  // (Written out rather than as a helper so the two soft edges stay visible.)
  float m0 = smoothstep(lo_ - soft, lo_ + soft, l0)
           * (1.0 - smoothstep(top - soft, top + soft, l0));

  // --- march --------------------------------------------------------------
  float len = (0.0045 + 0.055 * clamp(u_rms, 0.0, 1.0))
            * (0.60 + 0.60 * clamp(u_energy, 0.0, 1.0));
  vec2 stepv = axis * (len / float(STEPS));

  vec3 best = src;
  float bestL = l0;
  float alive = 1.0;

  for (int i = 1; i <= STEPS; i++) {
    vec2 sp = mirr(uv + stepv * float(i));
    vec3 c = texture(u_tex, sp).rgb;
    float l = luma(c);

    // The run survives only while the samples stay inside the interval.
    float inRun = smoothstep(lo_ - soft, lo_ + soft, l)
                * (1.0 - smoothstep(top - soft, top + soft, l));
    alive *= mix(1.0, inRun, 0.85);      // 15% leak: runs taper, never snap
    if (alive < 0.015) break;

    // Smooth "take the brighter one". Monotone in l, so the accumulator is
    // bounded by the brightest in-run sample and nothing else.
    float take = alive * smoothstep(0.0, 0.045, l - bestL);
    best = mix(best, c, take);
    bestL = mix(bestL, l, take);
  }

  // --- apply --------------------------------------------------------------
  float amount = m0 * (0.55 + 0.45 * clamp(u_intensity, 0.0, 1.0));
  vec3 col = mix(src, best, amount);

  // The smear flattens local contrast; give a little of it back by shading the
  // *head* of each run rather than by brightening its tail. Attenuation only —
  // the top of this term is exactly 1.0, so the pass stays non-expansive and
  // the interval's ceiling remains a true fixed point of the feedback loop.
  float edge = clamp(abs(bestL - l0) * 2.2, 0.0, 1.0);
  col = mix(col, col * (0.90 + 0.10 * edge), amount * 0.6);

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
