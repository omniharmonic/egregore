#version 300 es
// flow — vector-field displacement. Curl of an fbm field warps the image;
// strength <- low band, field direction drifts with time.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_low;
uniform float u_mid;
uniform float u_energy;
uniform float u_intensity;

out vec4 fragColor;

mat2 rot(float a) { float s = sin(a), c = cos(a); return mat2(c, -s, s, c); }

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 345.45));
  p += dot(p, p + 34.345);
  return fract(p.x * p.y);
}

float vnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));
  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float fbm(vec2 p) {
  float s = 0.0, a = 0.5;
  const mat2 m = mat2(1.62, 1.18, -1.18, 1.62);   // rotate+scale per octave
  for (int i = 0; i < 4; i++) {
    s += a * vnoise(p);
    p = m * p;
    a *= 0.5;
  }
  return s;
}

// Curl of a scalar field -> a divergence-free 2D flow, which advects without
// piling pixels up in sinks the way a raw gradient does.
vec2 curl(vec2 p) {
  const float e = 0.045;
  float nx1 = fbm(p + vec2(0.0, e));
  float nx2 = fbm(p - vec2(0.0, e));
  float ny1 = fbm(p + vec2(e, 0.0));
  float ny2 = fbm(p - vec2(e, 0.0));
  return vec2(nx1 - nx2, ny2 - ny1) / (2.0 * e);
}

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  vec2 p = (uv - 0.5) * vec2(ar, 1.0) * 2.3;
  p += vec2(u_time * 0.043, -u_time * 0.031);

  vec2 v = curl(p);
  // A second, larger and slower field so the warp has swell as well as detail.
  vec2 w = curl(p * 0.37 + vec2(-u_time * 0.017, u_time * 0.011));
  v = normalize(v + 1e-6) * 0.72 + w * 0.28;

  // The whole field slowly turns, so the drift direction never settles.
  v = rot(u_time * 0.061 + 1.9 * clamp(u_intensity, 0.0, 1.0)) * v;

  float amp = (0.0045 + 0.052 * clamp(u_low, 0.0, 1.0))
            * (0.62 + 0.45 * clamp(u_energy, 0.0, 1.0));

  vec2 duv = uv + v * amp * vec2(1.0 / ar, 1.0);
  vec3 col = texture(u_tex, mirr(duv)).rgb;

  // A faint counter-displaced ghost gives the warp body without a second pass.
  vec3 ghost = texture(u_tex, mirr(uv - v * amp * 0.55 * vec2(1.0 / ar, 1.0))).rgb;
  col = mix(col, max(col, ghost), 0.22 + 0.20 * clamp(u_mid, 0.0, 1.0));

  fragColor = vec4(col, 1.0);
}
