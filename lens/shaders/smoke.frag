#version 300 es
// smoke — volumetric advection with chromatic dispersion.
//
// flow warps once by a curl field. This walks the image backwards along that
// field in several steps, which is what separates a warp from a drift: each
// step carries a little more of where the pixel came from, so bright regions
// pull long streaks behind them and the whole frame behaves like something
// suspended in moving air rather than a picture being pushed around.
//
// The three colour channels are advected by slightly different amounts, so
// edges separate into spectra as they move — the refractive shimmer you get
// looking through smoke or heat, rather than a chromatic-aberration filter
// laid uniformly over everything.
//
// Audio: low sets how far a pixel travels, high sets the dispersion, onset
// blooms the density briefly, and intensity turns the whole field.
precision highp float;

uniform sampler2D u_tex;
uniform sampler2D u_prev;
uniform vec2  u_res;
uniform float u_time;
uniform float u_low;
uniform float u_mid;
uniform float u_high;
uniform float u_onset;
uniform float u_energy;
uniform float u_intensity;
uniform float u_centroid;

// Tunable: p0 drift, p1 dispersion, p2 persistence, p3 detail scale.
uniform float u_p0;
uniform float u_p1;
uniform float u_p2;
uniform float u_p3;

out vec4 fragColor;

const int STEPS = 6;

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

float hash(vec2 p) {
  p = fract(p * vec2(127.1, 311.7));
  p += dot(p, p + 27.31);
  return fract(p.x * p.y);
}

float vnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i),                hash(i + vec2(1.0, 0.0)), f.x),
             mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
}

// Two octaves is enough: the advection loop supplies the fine structure, and
// more octaves here only cost fill rate.
float fbm(vec2 p) {
  return 0.62 * vnoise(p) + 0.30 * vnoise(p * 2.17 + 11.3);
}

// Curl of the noise field — divergence-free, so the flow swirls and folds
// instead of piling up in sinks the way a plain gradient does.
vec2 curl(vec2 p) {
  float e = 0.0015;
  float n1 = fbm(p + vec2(0.0, e));
  float n2 = fbm(p - vec2(0.0, e));
  float n3 = fbm(p + vec2(e, 0.0));
  float n4 = fbm(p - vec2(e, 0.0));
  return vec2(n1 - n2, n4 - n3) / (2.0 * e);
}

mat2 rot(float a) { return mat2(cos(a), -sin(a), sin(a), cos(a)); }

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);
  vec2 aspect = vec2(1.0 / ar, 1.0);

  float drift = max(u_p0, 0.0001);
  float disp = u_p1;
  float persist = clamp(u_p2, 0.0, 0.98);
  float scale = max(u_p3, 0.05);

  vec2 p = uv * vec2(ar, 1.0) * scale;
  p += vec2(u_time * 0.037, -u_time * 0.028);

  vec2 v = normalize(curl(p) + 1e-6);
  // A slower, larger field underneath, so there is swell as well as detail.
  v = v * 0.7 + curl(p * 0.31 + vec2(-u_time * 0.013, u_time * 0.009)) * 0.3;
  v = rot(u_time * 0.043 + 2.2 * clamp(u_intensity, 0.0, 1.0)) * v;

  float travel = drift
    * (0.35 + 1.5 * clamp(u_low, 0.0, 1.0))
    * (0.6 + 0.7 * clamp(u_energy, 0.0, 1.0));

  // Walk backwards along the field, accumulating with a falling weight so
  // the near past dominates and the far past becomes haze.
  vec3 acc = vec3(0.0);
  float wsum = 0.0;
  float spread = disp * (0.25 + 1.2 * clamp(u_high, 0.0, 1.0));

  for (int i = 0; i < STEPS; i++) {
    float t = float(i) / float(STEPS - 1);
    float w = 1.0 - t * 0.82;
    vec2 step = v * travel * t * aspect;
    // Each channel travels a slightly different distance: colour separates
    // along the direction of motion, which is what smoke and heat haze do.
    float r = texture(u_tex, mirr(uv - step * (1.0 + spread))).r;
    float g = texture(u_tex, mirr(uv - step)).g;
    float b = texture(u_tex, mirr(uv - step * (1.0 - spread))).b;
    acc += vec3(r, g, b) * w;
    wsum += w;
  }
  vec3 col = acc / max(wsum, 1e-4);

  // Density bloom on an onset: the field briefly thickens where it is
  // already bright, so a beat reads as smoke catching light.
  float lum = dot(col, vec3(0.299, 0.587, 0.114));
  float bloom = clamp(u_onset, 0.0, 1.0) * smoothstep(0.35, 0.95, lum);
  col += col * bloom * 0.7;

  // Warm the highlights and cool the depths slightly by spectral centroid,
  // so a bright room and a dark one do not smoke the same colour.
  float tilt = clamp(u_centroid, 0.0, 1.0) - 0.5;
  col *= vec3(1.0 + tilt * 0.12, 1.0, 1.0 - tilt * 0.12);

  // Persistence against the previous frame gives the volume somewhere to
  // live: without it every frame re-smokes from scratch and it reads as
  // noise rather than as something continuous.
  vec3 prev = texture(u_prev, mirr(uv - v * travel * 0.12 * aspect)).rgb;
  col = mix(col, max(col, prev * 0.985), persist);

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
