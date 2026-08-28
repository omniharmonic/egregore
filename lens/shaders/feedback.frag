#version 300 es
// feedback — recursive trailing echo. Decay <- rms (louder = longer trails),
// slow zoom/rotation of the previous presented frame, subtle hue drift.
precision highp float;

uniform sampler2D u_tex;
uniform sampler2D u_prev;
uniform vec2  u_res;
uniform float u_time;
uniform float u_rms;
uniform float u_low;
uniform float u_energy;
uniform float u_intensity;

out vec4 fragColor;

mat2 rot(float a) { float s = sin(a), c = cos(a); return mat2(c, -s, s, c); }

// Rodrigues rotation about the grey axis — hue rotation without an HSV round trip.
vec3 hueRot(vec3 c, float a) {
  const vec3 k = vec3(0.57735026919);
  float cs = cos(a), sn = sin(a);
  return c * cs + cross(k, c) * sn + k * dot(k, c) * (1.0 - cs);
}

void main() {
  vec2 uv  = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);

  // Trail persistence. Quiet room -> short echo; loud room -> long smear.
  // A `max` blend is stable at any decay < 1, so the trail can be genuinely
  // long: ~0.75 fades in a third of a second, ~0.965 hangs for two seconds.
  float decay = mix(0.75, 0.955, clamp(u_rms * 1.7, 0.0, 1.0));
  decay = mix(decay, min(decay + 0.02, 0.972), clamp(u_intensity, 0.0, 1.0));

  // Each frame the echo is nudged inward and twisted a little; iterated over
  // many frames this is what makes the tunnel.
  float zoom = 1.0 - (0.0055 + 0.0110 * clamp(u_energy, 0.0, 1.0));
  float ang  = (0.0030 + 0.0075 * clamp(u_low, 0.0, 1.0)) * sin(u_time * 0.127);

  vec2 c = uv - 0.5;
  c.x *= ar;
  c = rot(ang) * c * zoom;
  c.x /= ar;
  vec2 puv = c + 0.5;

  // Soft edge guard: fade the echo out at the frame border instead of smearing
  // clamped edge texels into long streaks.
  vec2 g = smoothstep(vec2(0.0), vec2(0.02), puv) *
           smoothstep(vec2(0.0), vec2(0.02), 1.0 - puv);
  float edge = g.x * g.y;

  vec3 prev = texture(u_prev, clamp(puv, 0.0, 1.0)).rgb * edge;

  // A tiny hue turn per frame accumulates along the trail into a slow rainbow
  // wake, without ever tinting the live image.
  prev = hueRot(prev, 0.0060 + 0.0075 * sin(u_time * 0.083));

  vec3 cur  = texture(u_tex, uv).rgb;
  vec3 tr   = prev * decay;

  // Highlight ceiling — the one thing this shader must get right.
  //
  // u_prev is the *presented* frame, so every pass after this one is inside
  // the recursion. `bloom` has gain above 1 by design, which makes the loop
  // gain > 1: with only a decay < 1 the echo climbs a little every frame and
  // the picture reaches flat white in seconds (measured: mean luminance 39 ->
  // 135 and saturation 0.49 -> 0.12 for the stack flow>feedback>bloom).
  // Merely damping the top is not enough, because the runaway happens in the
  // midtones on the way up. So the echo is cut off entirely above a luminance
  // the loop is then unable to exceed. Stability no longer depends on knowing
  // what else is in the stack, which is the only version of this that can be
  // trusted with an arbitrary configured lens list.
  float pl = dot(prev, vec3(0.2126, 0.7152, 0.0722));
  float gate = 1.0 - smoothstep(0.30, 0.72, pl);
  tr *= gate;

  // Lighten, not add. A screen/additive blend looks right for one frame and is
  // wrong over a thousand: with the output fed back in, static bright content
  // creeps upward every frame and the picture drifts to milky pastel within
  // seconds. `max` has a fixed point exactly at the live image, so a still
  // frame is left alone and the echo is visible only where something *moved* —
  // which is what a trail actually is.
  vec3 col = max(cur, tr);

  // A restrained additive glint on top, proportional to how far the echo
  // exceeds the live frame. It decays with the trail, so it cannot accumulate.
  col += max(tr - cur, 0.0) * 0.22 * (1.0 - col) * gate;

  fragColor = vec4(col, 1.0);
}
