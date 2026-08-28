#version 300 es
// kaleidoscope — radial mirror. Segment count <- spectral centroid, blended
// between adjacent integer counts so it glides instead of popping.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_centroid;
uniform float u_high;
uniform float u_energy;
uniform float u_intensity;

out vec4 fragColor;

const float TAU = 6.28318530718;

// Mirror-repeat a uv so folded coordinates that leave [0,1] reflect back in
// rather than smearing the clamped border.
vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

// Fold the plane into n mirrored wedges around the centre.
vec2 fold(vec2 uv, float n, float spin, float ar) {
  vec2 p = uv - 0.5;
  p.x *= ar;
  float r = length(p);
  float a = atan(p.y, p.x) + spin;
  float sector = TAU / n;
  a = mod(a, sector);
  a = abs(a - sector * 0.5);        // mirror within the wedge
  vec2 q = vec2(cos(a), sin(a)) * r;
  q.x /= ar;
  return q + 0.5;
}

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  vec3 base = texture(u_tex, uv).rgb;

  // Bright, sibilant room -> finer symmetry. Dark, bassy room -> broad wedges.
  float seg = mix(3.0, 14.0, clamp(u_centroid, 0.0, 1.0));
  seg = clamp(seg, 3.0, 14.0);
  float n0 = floor(seg);
  float f  = seg - n0;
  // Each fold uses an *integer* wedge count (no angular seam); the fractional
  // part crossfades between the two, so the count moves continuously.
  f = f * f * (3.0 - 2.0 * f);

  float spin = u_time * (0.035 + 0.075 * clamp(u_intensity, 0.0, 1.0));

  // Slow radial breathing pulls detail in and out of the mandala.
  float breathe = 1.0 + 0.05 * sin(u_time * 0.21) + 0.06 * clamp(u_energy, 0.0, 1.0);

  vec2 a = fold(uv, n0,       spin, ar);
  vec2 b = fold(uv, n0 + 1.0, spin, ar);
  a = mirr((a - 0.5) / breathe + 0.5);
  b = mirr((b - 0.5) / breathe + 0.5);

  vec3 kal = mix(texture(u_tex, a).rgb, texture(u_tex, b).rgb, f);

  // Keep a little of the unfolded image at the very centre and the far corners
  // so the frame still reads as a place, not only a pattern.
  vec2 d = (uv - 0.5) * vec2(ar, 1.0);
  float r = length(d);
  float amount = smoothstep(0.035, 0.16, r) * (0.80 + 0.18 * clamp(u_high, 0.0, 1.0));

  vec3 col = mix(base, kal, clamp(amount, 0.0, 1.0));

  fragColor = vec4(col, 1.0);
}
