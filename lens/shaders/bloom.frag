#version 300 es
// bloom — bright pass, cheap 9-tap blur, additive overspill.
// Threshold is inversely bound to the high band: bright, airy sound lowers the
// threshold so more of the frame blooms.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_high;
uniform float u_rms;
uniform float u_energy;
uniform float u_valence;

out vec4 fragColor;

// 8 directions on a rotated ring + centre = 9 taps.
const vec2 K[8] = vec2[8](
  vec2( 1.0,  0.0), vec2( 0.7071,  0.7071),
  vec2( 0.0,  1.0), vec2(-0.7071,  0.7071),
  vec2(-1.0,  0.0), vec2(-0.7071, -0.7071),
  vec2( 0.0, -1.0), vec2( 0.7071, -0.7071)
);

vec3 bright(vec3 c, float thr, float knee) {
  float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
  // Soft knee so the bloom does not switch on at a hard luminance contour.
  float w = smoothstep(thr - knee, thr + knee, l);
  return c * w;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;
  vec2 px = 1.0 / u_res;

  vec3 src = texture(u_tex, uv).rgb;

  float hi  = clamp(u_high, 0.0, 1.0);
  // Inverse binding. The floor stays high enough that a bright clip does not
  // put the *whole* frame through the bright pass — bloom has to stay a
  // property of highlights, or the stack just lifts everything toward white.
  float thr = mix(0.82, 0.42, hi);
  float knee = 0.16;

  // Radius grows with loudness; the ring is sampled twice (inner + outer) for
  // a smoother falloff than a single ring of 8 gives.
  float rad = (2.2 + 6.0 * clamp(u_energy, 0.0, 1.0) + 2.5 * clamp(u_rms, 0.0, 1.0));

  vec3 sum = bright(src, thr, knee) * 0.28;
  float wsum = 0.28;

  float spin = u_time * 0.13;
  float cs = cos(spin), sn = sin(spin);
  mat2 rm = mat2(cs, -sn, sn, cs);

  for (int i = 0; i < 8; i++) {
    vec2 dir = rm * K[i];
    vec3 a = texture(u_tex, clamp(uv + dir * rad * px, 0.0, 1.0)).rgb;
    vec3 b = texture(u_tex, clamp(uv + dir * rad * 2.1 * px, 0.0, 1.0)).rgb;
    sum  += bright(a, thr, knee) * 0.070 + bright(b, thr, knee) * 0.040;
    wsum += 0.110;
  }
  vec3 glow = sum / wsum;

  // Warm the spill slightly on positive valence, cool it on negative — the
  // bloom is where a mood tint is least likely to look like a filter.
  vec3 tint = mix(vec3(0.90, 0.96, 1.10), vec3(1.10, 0.98, 0.88),
                  clamp(u_valence, 0.0, 1.0));

  float gain = 0.38 + 0.50 * hi;
  vec3 col = src + glow * tint * gain;

  // Screen the top end back under 1.0 so highlights bloom instead of clipping flat.
  col = 1.0 - (1.0 - clamp(src, 0.0, 1.0)) * (1.0 - clamp(col - src, 0.0, 1.0));

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
