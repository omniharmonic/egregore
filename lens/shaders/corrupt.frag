#version 300 es
// corrupt — deliberate texture corruption by feedback. Datamosh, not noise.
//
// A sparse set of macro-blocks stops reading the live frame and reads the
// previous *presented* frame instead. The block freezes: motion inside it
// stops while the rest of the picture keeps moving, which is precisely what a
// codec does when it loses an I-frame and keeps applying motion vectors. A
// slow creeping copy offset means the frozen region does not merely hold, it
// smears — each frame it copies from slightly further away, so the freeze
// walks and blurs the way real motion-vector residue does.
//
//   * Block grid is two-level (super-cells choose their own subdivision), so
//     the corruption is not a visible checkerboard.
//   * Blocks hold for 2-6 s and fade in and out at the ends of their life.
//   * Occasional channel-plane offset inside a frozen block.
//   * Density is bound to u_energy and reaches exactly zero below a floor —
//     a quiet room is a completely clean image. This is the whole reason the
//     effect reads as deliberate rather than as a broken renderer.
//
// Feedback safety — the important part. A block that samples u_prev is a
// closed loop through present(), so it must lose energy every frame or it will
// integrate present()'s grain and highlight handling into a runaway. Two
// mechanisms, both unconditional:
//
//   1. A per-frame decay, harshest in the highlights (the same luminance gate
//      feedback.frag uses), so the loop's gain is strictly below 1 everywhere
//      and far below 1 anywhere near white.
//   2. A slow bleed of the live frame back in, which gives the loop a fixed
//      point attached to the actual picture. A frozen block therefore relaxes
//      toward the live image over ~1.5 s even if its epoch never ended.
precision highp float;

uniform sampler2D u_tex;
uniform sampler2D u_prev;
uniform vec2  u_res;
uniform float u_time;
uniform float u_energy;
uniform float u_onset;
uniform float u_intensity;

out vec4 fragColor;

float luma(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }

float hash11(float n) {
  n = fract(n * 0.1031);
  n *= n + 33.33;
  n *= n + n;
  return fract(n);
}

float hash21(vec2 p) {
  vec3 p3 = fract(vec3(p.x, p.y, p.x) * 0.1031);
  p3 += dot(p3, p3.yzx + 33.33);
  return fract((p3.x + p3.y) * p3.z);
}

float hash31(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.yzx + 33.33);
  return fract((p.x + p.y) * p.z);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_res;
  float ar = u_res.x / max(u_res.y, 1.0);
  vec3 cur = texture(u_tex, uv).rgb;

  // Hard floor: below it the pass is an exact identity, not a faint version of
  // itself. Quiet room, clean image. The floor sits above the trough of the
  // no-feed idle LFO, so an unattended screen spends part of every cycle
  // completely uncorrupted rather than permanently lightly broken.
  //
  // This is the only branch in the shader, and it is uniform across the whole
  // frame (it reads uniforms only), so the texture fetches below stay in
  // uniform control flow.
  float amt = smoothstep(0.22, 0.50, clamp(u_energy, 0.0, 1.0));
  if (amt <= 0.0) {
    fragColor = vec4(cur, 1.0);
    return;
  }

  // --- two-level macro-block grid -----------------------------------------
  vec2 base = vec2(floor(7.0 * ar), 7.0);
  vec2 sc = floor(uv * base);                       // super-cell
  float divide = 1.0 + step(0.45, hash21(sc + 2.7)) + step(0.86, hash21(sc + 9.1));
  vec2 cellUv = fract(uv * base) * divide;
  vec2 bid = sc * 4.0 + floor(cellUv);

  // --- per-block life -----------------------------------------------------
  float rate = mix(0.17, 0.48, hash21(bid + 5.3));  // 2.1 - 5.9 s holds
  float ph = u_time * rate + hash21(bid + 21.7) * 7.0;
  float epoch = floor(ph);
  float life = 1.0 / rate;
  float age = fract(ph) * life;                     // seconds into this epoch

  float h = hash31(vec3(bid, epoch));
  // Sparse by construction: even at full energy roughly one block in six is
  // frozen, so the moving picture always dominates the stuck one.
  float density = (0.015 + 0.155 * amt) * (0.75 + 0.50 * clamp(u_onset, 0.0, 1.0));
  float stuck = step(1.0 - density, h);

  // Fade in and out at the ends of the block's life so regions arrive and
  // leave rather than blinking.
  float envl = smoothstep(0.0, 0.28, age) * (1.0 - smoothstep(life - 0.55, life, age));
  float k = stuck * clamp(envl, 0.0, 1.0);

  // --- creeping copy offset ------------------------------------------------
  float a = hash31(vec3(bid, epoch + 13.0)) * 6.28318530718;
  vec2 dir = vec2(cos(a), sin(a) * 0.55);
  vec2 crawl = dir * age * (0.0035 + 0.0090 * amt) * vec2(1.0 / ar, 1.0);

  vec2 puv = clamp(uv + crawl, 0.0, 1.0);
  vec3 p = texture(u_prev, puv).rgb;

  // --- occasional channel-plane offset -------------------------------------
  // Selected by mix rather than by a branch: `texture` inside non-uniform
  // control flow is only well defined for non-mipmapped samplers, and this
  // shader should not depend on that.
  float chan = step(0.70, hash11(dot(bid, vec2(12.9898, 78.233)) + epoch));
  vec2 co = vec2(-dir.y, dir.x) * (0.0035 + 0.0060 * amt) * vec2(1.0 / ar, 1.0);
  float pr = texture(u_prev, clamp(puv + co, 0.0, 1.0)).r;
  float pb = texture(u_prev, clamp(puv - co, 0.0, 1.0)).b;
  p.r = mix(p.r, pr, chan);
  p.b = mix(p.b, pb, chan);

  // --- loop cap (see header) ------------------------------------------------
  float pl = luma(p);
  float gate = 1.0 - smoothstep(0.34, 0.78, pl);
  float decay = mix(0.900, 0.998, gate);
  float bleed = 0.010 + 0.100 * (1.0 - gate);
  p = mix(p * decay, cur, bleed);

  // --- composite ------------------------------------------------------------
  vec3 col = mix(cur, p, k);

  // Block edges: a codec's frozen macro-block has a visible boundary, but only
  // where it differs from what is behind it. Purely subtractive.
  vec2 e = abs(fract(cellUv) - 0.5);
  float border = smoothstep(0.44, 0.499, max(e.x, e.y));
  col *= 1.0 - 0.16 * k * border * clamp(abs(luma(p) - luma(cur)) * 3.0, 0.0, 1.0)
             * (0.5 + 0.5 * clamp(u_intensity, 0.0, 1.0));

  fragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}
