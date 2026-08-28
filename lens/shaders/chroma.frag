#version 300 es
// chroma — RGB channel separation along a drifting vector. The offset spikes
// on onset; the relaxation envelope lives in JS (u_onset arrives already
// smoothed with a fast attack and a ~1.2 s release), so this shader is pure.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_onset;
uniform float u_high;
uniform float u_centroid;
uniform float u_intensity;

out vec4 fragColor;

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  // Separation axis wanders: a slow rotation plus a nudge from timbre, so
  // repeated hits do not always tear the image the same way.
  float ang = u_time * 0.19 + 2.4 * clamp(u_centroid, 0.0, 1.0);
  vec2 dir = vec2(cos(ang), sin(ang));

  float onset = clamp(u_onset, 0.0, 1.0);
  float off = 0.0012                       // always a hair of fringing
            + 0.0180 * onset * onset       // squared: transients read as hits
            + 0.0042 * clamp(u_high, 0.0, 1.0);

  // Scale with radius so the centre stays readable and the edges disperse,
  // the way a real lens does.
  vec2 d = (uv - 0.5) * vec2(ar, 1.0);
  float r = clamp(length(d) * 1.35, 0.0, 1.0);
  float k = off * (0.35 + 0.95 * r);

  vec2 step = dir * k * vec2(1.0 / ar, 1.0);

  float rr = texture(u_tex, mirr(uv + step)).r;
  vec3  gg = texture(u_tex, uv).rgb;
  float bb = texture(u_tex, mirr(uv - step)).b;

  vec3 col = vec3(rr, gg.g, bb);

  // Recover a little of the energy the split throws away, and lift saturation
  // with intensity so a hard moment reads as colour, not just misregistration.
  float lum = dot(col, vec3(0.2126, 0.7152, 0.0722));
  float sat = 1.0 + 0.30 * onset + 0.16 * clamp(u_intensity, 0.0, 1.0);
  col = mix(vec3(lum), col, sat);

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
