/* Dense map: one GPU point per identity, with bounded overlays and spatial picking. */
(function (root) {
  'use strict';
  class Index {
    constructor(nodes, cell = 64) {
      this.cell = cell; this.cells = new Map(); this.nodes = nodes; this.maxRadius = 0;
      for (const n of nodes) {
        this.maxRadius = Math.max(this.maxRadius, n.r || 0);
        const key = `${Math.floor(n.x / cell)},${Math.floor(n.y / cell)}`;
        let bucket = this.cells.get(key); if (!bucket) this.cells.set(key, bucket = []);
        bucket.push(n);
      }
    }
    query(x0, y0, x1, y1, limit = Infinity) {
      const out = [], add = bucket => { for (const n of bucket || []) {
        if (n.x >= x0 && n.x <= x1 && n.y >= y0 && n.y <= y1) out.push(n);
        if (out.length >= limit) return true;
      } return false; };
      const ax = Math.floor(x0 / this.cell), ay = Math.floor(y0 / this.cell), bx = Math.floor(x1 / this.cell), by = Math.floor(y1 / this.cell);
      if ((bx - ax + 1) * (by - ay + 1) > this.cells.size * 2) {
        for (const bucket of this.cells.values()) if (add(bucket)) break;
      } else outer: for (let x = ax; x <= bx; x++) for (let y = ay; y <= by; y++) if (add(this.cells.get(`${x},${y}`))) break outer;
      return out;
    }
    pick(x, y, scale) {
      const pad = 7 / scale, radius = this.maxRadius + pad;
      let best = null, score = Infinity;
      for (const n of this.query(x - radius, y - radius, x + radius, y + radius)) {
        const r = Math.max(n.r, 2.4 / scale), d = Math.hypot(n.x - x, n.y - y);
        if (d <= Math.max(r + 3 / scale, pad) && d - r < score) { best = n; score = d - r; }
      }
      return best;
    }
  }
  class Points {
    constructor(canvas) {
      this.canvas = canvas;
      const gl = this.gl = canvas.getContext('webgl', { alpha: true, antialias: false, depth: false, preserveDrawingBuffer: false });
      if (!gl) throw new Error('WebGL unavailable');
      this.data = new Float32Array(0); this.count = 0; this.lost = true;
      this.initialize();
      canvas.addEventListener('webglcontextlost', e => { e.preventDefault(); this.lost = true; });
      canvas.addEventListener('webglcontextrestored', () => {
        // A restored context invalidates every old shader, program and buffer.
        // Keep the CPU fallback if rebuilding fails; never reuse stale resources.
        try { this.initialize(); } catch (_) { this.lost = true; }
      });
    }
    initialize() {
      const gl = this.gl;
      this.lost = true;
      const shader = (type, text) => { const s = gl.createShader(type); gl.shaderSource(s, text); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; };
      const p = this.program = gl.createProgram();
      gl.attachShader(p, shader(gl.VERTEX_SHADER, 'attribute vec3 point; uniform vec2 viewport; uniform vec3 camera; uniform float dpr; void main(){ vec2 p=point.xy*camera.z+camera.xy; gl_Position=vec4(p.x/viewport.x*2.0-1.0,1.0-p.y/viewport.y*2.0,0,1); gl_PointSize=max(2.0,point.z*camera.z*2.0)*dpr; }'));
      gl.attachShader(p, shader(gl.FRAGMENT_SHADER, 'precision mediump float; uniform vec4 color; void main(){vec2 p=gl_PointCoord*2.0-1.0; if(dot(p,p)>1.0)discard;gl_FragColor=color;}'));
      gl.linkProgram(p); if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
      this.buffer = gl.createBuffer();
      if (!this.buffer) throw new Error('WebGL buffer unavailable');
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer); gl.bufferData(gl.ARRAY_BUFFER, this.data, gl.STATIC_DRAW);
      this.lost = gl.isContextLost();
    }
    set(nodes) {
      const data = new Float32Array(nodes.length * 3);
      nodes.forEach((n, i) => { data[i * 3] = n.x; data[i * 3 + 1] = n.y; data[i * 3 + 2] = n.r; });
      // Preserve the newest coordinates even when set() runs during context loss.
      this.data = data; this.count = nodes.length;
      if (this.lost || this.gl.isContextLost()) return;
      const gl = this.gl; gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    }
    draw(w, h, x, y, k, color) {
      if (this.lost || this.gl.isContextLost()) return false;
      const gl = this.gl, dpr = Math.min(root.devicePixelRatio || 1, 2), p = this.program;
      if (this.canvas.width !== Math.round(w * dpr) || this.canvas.height !== Math.round(h * dpr)) { this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr); }
      gl.viewport(0, 0, this.canvas.width, this.canvas.height); gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); gl.useProgram(p);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer); const a = gl.getAttribLocation(p, 'point'); gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 3, gl.FLOAT, false, 0, 0);
      gl.uniform2f(gl.getUniformLocation(p, 'viewport'), w, h); gl.uniform3f(gl.getUniformLocation(p, 'camera'), x, y, k); gl.uniform1f(gl.getUniformLocation(p, 'dpr'), dpr);
      gl.uniform4fv(gl.getUniformLocation(p, 'color'), color); gl.drawArrays(gl.POINTS, 0, this.count); return true;
    }
  }
  root.DenseMap = { Index, Points };
  if (typeof module !== 'undefined') module.exports = root.DenseMap;
})(typeof window === 'undefined' ? globalThis : window);
