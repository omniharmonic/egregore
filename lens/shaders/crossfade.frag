#version 300 es
// crossfade — pass 0. Two video textures, cover-fit, never a hard cut.
precision highp float;

uniform sampler2D u_videoA;
uniform sampler2D u_videoB;
uniform vec2  u_res;
uniform vec2  u_sizeA;   // intrinsic pixel size of video A (0 if unknown)
uniform vec2  u_sizeB;
uniform float u_mix;     // 0 = A, 1 = B
uniform float u_time;

out vec4 fragColor;

// Map screen uv into source uv so the source *covers* the viewport
// (centre crop, aspect preserved) rather than stretching.
vec2 cover(vec2 uv, vec2 res, vec2 src) {
  if (src.x < 2.0 || src.y < 2.0) return uv;          // size not known yet
  float ra = res.x / max(res.y, 1.0);
  float sa = src.x / max(src.y, 1.0);
  vec2 s = (sa > ra) ? vec2(ra / sa, 1.0) : vec2(1.0, sa / ra);
  return (uv - 0.5) * s + 0.5;
}

// Before the first clip exists — an operator setting up at seven, or a total
// cold start with an empty cache — the screen should be alive but almost dark,
// not dead. Deep, slow, barely there; the lens stack has something to work on.
vec3 ember(vec2 uv, float ar, float t) {
  vec2 p = (uv - 0.5) * vec2(ar, 1.0);
  float r = length(p);
  float a = atan(p.y, p.x);
  float swell = 0.5 + 0.5 * sin(t * 0.11 + r * 3.1 + sin(a * 2.0 + t * 0.07));
  float glow = smoothstep(0.95, 0.05, r) * (0.020 + 0.026 * swell);
  vec3 col = mix(vec3(0.06, 0.05, 0.10), vec3(0.14, 0.09, 0.06), swell) * glow * 6.0;
  return col + vec3(0.008, 0.008, 0.012);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;

  if (u_sizeA.x < 2.0 && u_sizeB.x < 2.0) {
    fragColor = vec4(ember(uv, u_res.x / max(u_res.y, 1.0), u_time), 1.0);
    return;
  }

  vec3 a = texture(u_videoA, clamp(cover(uv, u_res, u_sizeA), 0.0, 1.0)).rgb;
  vec3 b = texture(u_videoB, clamp(cover(uv, u_res, u_sizeB), 0.0, 1.0)).rgb;

  // Smootherstep on the mix so the fade has no perceptual corner at either end.
  float m = clamp(u_mix, 0.0, 1.0);
  m = m * m * m * (m * (m * 6.0 - 15.0) + 10.0);

  // Blend in linear-ish space: a plain lerp of gamma values dips in the middle
  // and reads as a dark "swipe". Squaring approximates linear light cheaply.
  vec3 col = sqrt(mix(a * a, b * b, m));

  fragColor = vec4(col, 1.0);
}
