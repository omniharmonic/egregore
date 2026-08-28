#version 300 es
// present — final pass. Gentle vignette, very subtle film grain, and a 1–2%
// valence-driven colour grade. Nothing here should be nameable on screen.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_valence;
uniform float u_energy;
uniform float u_rms;

out vec4 fragColor;

float hash(vec2 p) {
  p = fract(p * vec2(443.897, 441.423));
  p += dot(p, p + 19.19);
  return fract(p.x * p.y);
}

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  vec3 col = texture(u_tex, uv).rgb;

  // --- grade: 1–2%, cool at low valence, warm at high ---
  float v = clamp(u_valence, 0.0, 1.0);
  vec3 grade = mix(vec3(0.982, 0.994, 1.018), vec3(1.018, 0.998, 0.978), v);
  col *= grade;

  // A whisper of filmic contrast so the stack's midtones do not go flat.
  col = clamp(col, 0.0, 1.0);
  col = col * col * (3.0 - 2.0 * col) * 0.14 + col * 0.86;

  // Highlight shoulder. Engages only near the top of the range, so a stack
  // that has been generous (bloom over a bright clip) rolls off instead of
  // flattening into a white plateau. Midtones are untouched.
  float hl = dot(col, vec3(0.2126, 0.7152, 0.0722));
  col /= 1.0 + 0.55 * smoothstep(0.72, 1.0, hl);

  // --- vignette ---
  vec2 d = (uv - 0.5) * vec2(ar, 1.0);
  float r = length(d) / 0.72;
  float vig = 1.0 - 0.30 * smoothstep(0.55, 1.35, r);
  // Loud moments open the frame up a little.
  vig = mix(vig, 1.0, 0.28 * clamp(u_rms, 0.0, 1.0));
  col *= vig;

  // --- grain ---
  // Animated per frame; amplitude scales down in the highlights, as on film.
  float n = hash(gl_FragCoord.xy + vec2(fract(u_time * 61.7) * 743.1,
                                        fract(u_time * 37.3) * 219.7));
  float lum = dot(col, vec3(0.2126, 0.7152, 0.0722));
  float gAmp = 0.026 * (1.0 - 0.55 * lum) * (0.85 + 0.30 * clamp(u_energy, 0.0, 1.0));
  col += (n - 0.5) * gAmp;

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
