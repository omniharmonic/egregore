#version 300 es
// liquid — refractive surface. Layered sine + value-noise height field, its
// gradient used as a normal to refract the image. Viscosity <- mid band:
// more mid means thicker, slower, longer-wavelength swell.
precision highp float;

uniform sampler2D u_tex;
uniform vec2  u_res;
uniform float u_time;
uniform float u_mid;
uniform float u_low;
uniform float u_high;
uniform float u_energy;

out vec4 fragColor;

vec2 mirr(vec2 v) { return abs(fract(v * 0.5) * 2.0 - 1.0); }

float hash(vec2 p) {
  p = fract(p * vec2(127.1, 311.7));
  p += dot(p, p + 27.31);
  return fract(p.x * p.y);
}

float vnoise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i),               hash(i + vec2(1.0, 0.0)), f.x),
             mix(hash(i + vec2(0.0,1.0)), hash(i + vec2(1.0, 1.0)), f.x), f.y);
}

// Surface height: three crossed travelling waves plus a noise layer.
float height(vec2 p, float t, float visc) {
  float k = mix(4.6, 1.9, visc);        // thicker fluid -> longer wavelength
  float sp = mix(1.00, 0.42, visc);     // and slower
  float h = 0.0;
  h += sin(p.x * k        + t * 0.71 * sp) * 0.50;
  h += sin(p.y * k * 0.83 - t * 0.53 * sp) * 0.42;
  h += sin((p.x + p.y) * k * 0.61 + t * 0.37 * sp) * 0.34;
  h += sin((p.x - p.y) * k * 1.37 - t * 0.91 * sp) * 0.18;
  h += (vnoise(p * k * 0.42 + vec2(t * 0.06 * sp, -t * 0.045 * sp)) - 0.5) * 1.35;
  return h;
}

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  float visc = clamp(u_mid, 0.0, 1.0);
  vec2 p = (uv - 0.5) * vec2(ar, 1.0) * 3.2;
  float t = u_time;

  // Finite-difference gradient -> surface normal.
  const float e = 0.012;
  float h  = height(p, t, visc);
  float hx = height(p + vec2(e, 0.0), t, visc) - height(p - vec2(e, 0.0), t, visc);
  float hy = height(p + vec2(0.0, e), t, visc) - height(p - vec2(0.0, e), t, visc);
  vec2 grad = vec2(hx, hy) / (2.0 * e);

  float bump = 0.020 + 0.030 * visc + 0.018 * clamp(u_low, 0.0, 1.0);
  vec3 n = normalize(vec3(-grad * bump, 1.0));

  // Refraction: displace along the normal's tangential part. Amplitude falls
  // as viscosity rises (thick fluid deforms less per unit of surface slope).
  float amp = (0.016 + 0.030 * clamp(u_energy, 0.0, 1.0)) * mix(1.25, 0.70, visc);
  vec2 duv = uv + n.xy * amp * vec2(1.0 / ar, 1.0);

  // Tiny dispersion across channels reads as real glass.
  float disp = 0.0018 * (0.5 + clamp(u_high, 0.0, 1.0));
  vec2 dd = n.xy * disp * vec2(1.0 / ar, 1.0);
  vec3 col;
  col.r = texture(u_tex, mirr(duv + dd)).r;
  col.g = texture(u_tex, mirr(duv)).g;
  col.b = texture(u_tex, mirr(duv - dd)).b;

  // Slow specular shimmer from a light that drifts across the surface.
  vec3 L = normalize(vec3(cos(t * 0.11) * 0.55, sin(t * 0.083) * 0.55, 0.82));
  vec3 V = vec3(0.0, 0.0, 1.0);
  vec3 H = normalize(L + V);
  float spec = pow(max(dot(n, H), 0.0), mix(38.0, 96.0, visc));
  float fres = pow(1.0 - max(n.z, 0.0), 2.2);

  col += spec * (0.16 + 0.24 * clamp(u_high, 0.0, 1.0));
  col += fres * 0.045 * vec3(0.75, 0.86, 1.0);

  // Faint depth shading from the height field itself, so the sheet has volume.
  col *= 0.955 + 0.055 * clamp(h * 0.5 + 0.5, 0.0, 1.0);

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
