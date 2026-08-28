// gl.js — WebGL2 pipeline: fullscreen triangle, ping-pong FBO lens stack,
// persistent feedback texture. No dependencies, no throwing on failure.

// Sensible starting values per lens, so a shader that reads u_p0..u_p3
// looks right before anyone touches a control.
export const LENS_PARAM_DEFAULTS = {
  smoke:        [0.06, 0.35, 0.55, 3.2],
  flow:         [0.05, 0.30, 0.00, 2.6],
  feedback:     [0.94, 0.02, 0.00, 0.0],
  liquid:       [0.55, 0.40, 0.00, 2.0],
  bloom:        [0.55, 0.35, 0.00, 0.0],
  chroma:       [0.35, 0.00, 0.00, 0.0],
  glitch:       [0.35, 0.30, 0.00, 0.0],
  kaleidoscope: [6.00, 0.00, 0.00, 0.0],
  pixelsort:    [0.50, 0.35, 0.00, 0.0],
  crt:          [0.35, 0.45, 0.00, 0.0],
  corrupt:      [0.35, 0.30, 0.00, 0.0],
};

const AUDIO_UNIFORMS = [
  'u_rms', 'u_low', 'u_mid', 'u_high', 'u_centroid',
  'u_onset', 'u_energy', 'u_valence', 'u_intensity',
  'u_p0', 'u_p1', 'u_p2', 'u_p3',
];

const UNIFORMS = [
  'u_tex', 'u_prev', 'u_res', 'u_time', 'u_mix',
  'u_videoA', 'u_videoB', 'u_sizeA', 'u_sizeB', ...AUDIO_UNIFORMS,
];

// gl_VertexID-driven fullscreen triangle: no buffers, no attributes.
const VERT = `#version 300 es
void main() {
  vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}`;

export class Renderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.gl = null;
    this.sources = new Map();   // name -> frag source
    this.programs = new Map();  // name -> {prog, loc}
    this.targets = [];          // 4 render targets: 0,1 stack ping-pong; 2,3 present/feedback
    this.presentIdx = 0;
    this.w = 0; this.h = 0;
    this.videoTex = [null, null];
  }

  init() {
    const opts = {
      alpha: false, antialias: false, depth: false, stencil: false,
      premultipliedAlpha: false, preserveDrawingBuffer: true,
      powerPreference: 'high-performance', desynchronized: false,
    };
    let gl = null;
    try { gl = this.canvas.getContext('webgl2', opts); } catch { gl = null; }
    if (!gl) return false;
    this.gl = gl;
    this.vao = gl.createVertexArray(); // required: draws need *some* VAO bound
    gl.disable(gl.DEPTH_TEST);
    gl.disable(gl.BLEND);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    this.videoTex = [this._tex(), this._tex()];
    for (const [name, src] of this.sources) this._build(name, src);
    this.resize(this.w || 2, this.h || 2, true);
    return true;
  }

  // --- resource helpers -------------------------------------------------

  _tex() {
    const gl = this.gl;
    const t = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE,
      new Uint8Array([0, 0, 0, 255]));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  }

  _compile(type, src, name) {
    const gl = this.gl;
    const sh = gl.createShader(type);
    gl.shaderSource(sh, src);
    gl.compileShader(sh);
    if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
      console.warn(`[lens] shader "${name}" failed:`, gl.getShaderInfoLog(sh));
      gl.deleteShader(sh);
      return null;
    }
    return sh;
  }

  _build(name, frag) {
    const gl = this.gl;
    const vs = this._compile(gl.VERTEX_SHADER, VERT, name + ':vert');
    const fs = this._compile(gl.FRAGMENT_SHADER, frag, name);
    if (!vs || !fs) { if (vs) gl.deleteShader(vs); if (fs) gl.deleteShader(fs); return false; }
    const prog = gl.createProgram();
    gl.attachShader(prog, vs); gl.attachShader(prog, fs); gl.linkProgram(prog);
    gl.deleteShader(vs); gl.deleteShader(fs);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      console.warn(`[lens] link "${name}" failed:`, gl.getProgramInfoLog(prog));
      gl.deleteProgram(prog);
      return false;
    }
    const loc = {};
    for (const u of UNIFORMS) loc[u] = gl.getUniformLocation(prog, u);
    this.programs.set(name, { prog, loc });
    return true;
  }

  /** Register (and compile, if the context is live) a fragment shader. */
  addShader(name, frag) {
    this.sources.set(name, frag);
    if (this.gl) return this._build(name, frag);
    return true;
  }

  has(name) { return this.programs.has(name); }

  resize(w, h, force) {
    const gl = this.gl;
    if (!gl) { this.w = w; this.h = h; return; }
    w = Math.max(2, w | 0); h = Math.max(2, h | 0);
    if (!force && w === this.w && h === this.h && this.targets.length) return;
    this.w = w; this.h = h;
    for (const t of this.targets) { gl.deleteFramebuffer(t.fbo); gl.deleteTexture(t.tex); }
    this.targets = [];
    for (let i = 0; i < 4; i++) {
      const tex = this._tex();
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, w, h, 0, gl.RGBA, gl.UNSIGNED_BYTE, null);
      const fbo = gl.createFramebuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER, fbo);
      gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
      gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT);
      this.targets.push({ tex, fbo });
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  /** Upload a <video> frame into texture slot 0 or 1. */
  uploadVideo(slot, video) {
    const gl = this.gl;
    if (!gl || !video || video.readyState < 2 || !video.videoWidth) return false;
    try {
      gl.bindTexture(gl.TEXTURE_2D, this.videoTex[slot]);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video);
      return true;
    } catch { return false; }
  }

  // --- the frame --------------------------------------------------------

  _pass(name, target, inTex, prevTex, s) {
    const gl = this.gl;
    const p = this.programs.get(name);
    if (!p) return false;
    gl.bindFramebuffer(gl.FRAMEBUFFER, target ? target.fbo : null);
    gl.viewport(0, 0, this.w, this.h);
    gl.useProgram(p.prog);
    const L = p.loc;

    if (L.u_tex) { gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, inTex); gl.uniform1i(L.u_tex, 0); }
    if (L.u_prev) { gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, prevTex); gl.uniform1i(L.u_prev, 1); }
    if (L.u_videoA) { gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D, this.videoTex[0]); gl.uniform1i(L.u_videoA, 2); }
    if (L.u_videoB) { gl.activeTexture(gl.TEXTURE3); gl.bindTexture(gl.TEXTURE_2D, this.videoTex[1]); gl.uniform1i(L.u_videoB, 3); }

    if (L.u_res) gl.uniform2f(L.u_res, this.w, this.h);
    if (L.u_time) gl.uniform1f(L.u_time, s.time);
    if (L.u_mix) gl.uniform1f(L.u_mix, s.mix);
    if (L.u_sizeA) gl.uniform2f(L.u_sizeA, s.sizeA[0], s.sizeA[1]);
    if (L.u_sizeB) gl.uniform2f(L.u_sizeB, s.sizeB[0], s.sizeB[1]);
    for (const u of AUDIO_UNIFORMS) if (L[u]) gl.uniform1f(L[u], s.audio[u.slice(2)] || 0);
    // Per-lens tuning. Defaults live in the shader's own fallbacks; anything
    // the operator has set for this lens arrives here as p0..p3.
    const P = (s.params && s.params[name]) || null;
    for (let i = 0; i < 4; i++) {
      const loc = L['u_p' + i];
      if (loc) gl.uniform1f(loc, P && P[i] !== undefined ? P[i] : LENS_PARAM_DEFAULTS[name]?.[i] ?? 0.5);
    }

    gl.bindVertexArray(this.vao);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    return true;
  }

  /**
   * @param {string[]} stack lens names, already trimmed to the active count
   * @returns {number} number of passes actually drawn (crossfade + lenses + present)
   */
  render(stack, s) {
    const gl = this.gl;
    if (!gl || gl.isContextLost() || this.targets.length < 4) return 0;

    const prevTex = this.targets[2 + (1 - this.presentIdx)].tex;
    let src = this.targets[0], dst = this.targets[1];
    let n = 0;

    if (!this._pass('crossfade', src, this.videoTex[0], prevTex, s)) return 0;
    n++;

    for (const name of stack) {
      if (!this.programs.has(name)) continue;
      if (this._pass(name, dst, src.tex, prevTex, s)) {
        const t = src; src = dst; dst = t;
        n++;
      }
    }

    const present = this.targets[2 + this.presentIdx];
    if (!this._pass('present', present, src.tex, prevTex, s)) {
      // Present shader missing: blit the stack output straight out.
      this._blit(src.fbo);
      return n;
    }
    n++;
    this._blit(present.fbo);
    // The frame we just presented becomes u_prev for the next one.
    this.presentIdx = 1 - this.presentIdx;
    return n;
  }

  _blit(fbo) {
    const gl = this.gl;
    const dw = this.canvas.width, dh = this.canvas.height;
    gl.bindFramebuffer(gl.READ_FRAMEBUFFER, fbo);
    gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, null);
    gl.blitFramebuffer(0, 0, this.w, this.h, 0, 0, dw, dh, gl.COLOR_BUFFER_BIT, gl.LINEAR);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  }

  /** Drop every GPU object; init() rebuilds from this.sources. */
  forget() {
    this.programs.clear();
    this.targets = [];
    this.videoTex = [null, null];
    this.gl = null;
  }
}
