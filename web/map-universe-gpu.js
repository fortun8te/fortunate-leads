/* Instanced circle quads. Each resident tile has its own bounded GPU buffer. */
(function (root) {
  "use strict";
  const SIZE = 1024,
    CELL = 64,
    COLS = 16;
  const GLSL_VERTEX = `#version 300 es
precision highp float;
layout(location=0) in vec3 position;
layout(location=1) in vec4 color;
layout(location=2) in float slot;
uniform vec4 camera0; uniform vec4 camera1;
out vec2 uv; out vec4 tint; out float atlasSlot; out float radius;
void main(){vec2 corners[6]=vec2[6](vec2(-1,-1),vec2(1,-1),vec2(-1,1),vec2(-1,1),vec2(1,-1),vec2(1,1));vec2 c=corners[gl_VertexID];float r=color.a<0.?13.:max(1.1,position.z*camera1.z);vec2 p=(position.xy-camera0.xy)*camera1.z+camera0.zw+c*r;gl_Position=vec4(p.x/camera1.x*2.-1.,1.-p.y/camera1.y*2.,0,1);uv=c;float natural=position.z*camera1.z;float coverage=color.a<0.?1.:clamp(natural*natural/(1.1*1.1),.18,1.);vec3 ink=mix(vec3(.9),vec3(.48),color.rgb);tint=vec4(ink,abs(color.a)*coverage);atlasSlot=slot;radius=r;}`;
  const GLSL_FRAGMENT = `#version 300 es
precision highp float;
in vec2 uv; in vec4 tint; in float atlasSlot; in float radius;
uniform sampler2D atlas;out vec4 outColor;
void main(){float d=length(uv);if(d>1.)discard;float a=1.-smoothstep(1.-clamp(.75/max(radius,1.),.06,.45),1.,d);vec4 c=tint;if(atlasSlot>=0.&&radius>=12.){vec2 cell=vec2(mod(atlasSlot,16.),floor(atlasSlot/16.));vec2 tex=(cell*64.+vec2(1.)+(uv*.5+.5)*62.)/1024.;c=texture(atlas,tex);}outColor=vec4(c.rgb,c.a*a);}`;
  const WGSL = `struct Camera { a:vec4f,b:vec4f }; @group(0) @binding(0) var<uniform> camera:Camera;
@group(0) @binding(1) var atlas:texture_2d<f32>; @group(0) @binding(2) var atlasSampler:sampler;
struct Out { @builtin(position) pos:vec4f,@location(0) uv:vec2f,@location(1) tint:vec4f,@location(2) slot:f32,@location(3) radius:f32 };
@vertex fn vs(@builtin(vertex_index) vertex:u32,@location(0) position:vec3f,@location(1) color:vec4f,@location(2) slot:f32)->Out {var corners=array<vec2f,6>(vec2f(-1,-1),vec2f(1,-1),vec2f(-1,1),vec2f(-1,1),vec2f(1,-1),vec2f(1,1));let c=corners[vertex];let r=select(max(1.1,position.z*camera.b.z),13.,color.a<0.);let p=(position.xy-camera.a.xy)*camera.b.z+camera.a.zw+c*r;var o:Out;o.pos=vec4f(p.x/camera.b.x*2.-1.,1.-p.y/camera.b.y*2.,0,1);o.uv=c;let natural=position.z*camera.b.z;let coverage=select(clamp(natural*natural/(1.1*1.1),.18,1.),1.,color.a<0.);let ink=mix(vec3f(.9),vec3f(.48),color.rgb);o.tint=vec4f(ink,abs(color.a)*coverage);o.slot=slot;o.radius=r;return o;}
@fragment fn fs(o:Out)->@location(0) vec4f {let d=length(o.uv);if(d>1.){discard;}let a=1.-smoothstep(1.-clamp(.75/max(o.radius,1.),.06,.45),1.,d);var c=o.tint;if(o.slot>=0.&&o.radius>=12.){let cell=vec2f(o.slot%16.,floor(o.slot/16.));let uv=(cell*64.+vec2f(1.)+(o.uv*.5+.5)*62.)/1024.;c=textureSampleLevel(atlas,atlasSampler,uv,0.);}return vec4f(c.rgb,c.a*a);}`;
  function uniforms(cam) {
    return new Float32Array([
      cam.cx,
      cam.cy,
      ...cam.mid,
      cam.w,
      cam.h,
      cam.scale,
      0,
    ]);
  }
  class GLRenderer {
    constructor(canvas, onFailure) {
      this.canvas = canvas;
      this.gl = canvas.getContext("webgl2", {
        alpha: true,
        antialias: false,
        premultipliedAlpha: false,
      });
      if (!this.gl) throw Error("Map graphics unavailable");
      const gl = this.gl;
      const shader = (type, source) => {
        const s = gl.createShader(type);
        gl.shaderSource(s, source);
        gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
          const msg = gl.getShaderInfoLog(s);
          gl.deleteShader(s);
          throw Error(msg);
        }
        return s;
      };
      const v = shader(gl.VERTEX_SHADER, GLSL_VERTEX),
        f = shader(gl.FRAGMENT_SHADER, GLSL_FRAGMENT);
      this.program = gl.createProgram();
      gl.attachShader(this.program, v);
      gl.attachShader(this.program, f);
      gl.linkProgram(this.program);
      gl.deleteShader(v);
      gl.deleteShader(f);
      if (!gl.getProgramParameter(this.program, gl.LINK_STATUS))
        throw Error(gl.getProgramInfoLog(this.program));
      this.a = gl.getUniformLocation(this.program, "camera0");
      this.b = gl.getUniformLocation(this.program, "camera1");
      this.texture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.texImage2D(
        gl.TEXTURE_2D,
        0,
        gl.RGBA,
        SIZE,
        SIZE,
        0,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        null,
      );
      for (const p of [gl.TEXTURE_MIN_FILTER, gl.TEXTURE_MAG_FILTER])
        gl.texParameteri(gl.TEXTURE_2D, p, gl.LINEAR);
      for (const p of [gl.TEXTURE_WRAP_S, gl.TEXTURE_WRAP_T])
        gl.texParameteri(gl.TEXTURE_2D, p, gl.CLAMP_TO_EDGE);
      this.lost = (e) => {
        e.preventDefault();
        onFailure(Error("Map graphics reset"));
      };
      canvas.addEventListener("webglcontextlost", this.lost);
    }
    upload(tile) {
      const gl = this.gl;
      tile.gpu = { buffer: gl.createBuffer(), vao: gl.createVertexArray() };
      gl.bindVertexArray(tile.gpu.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, tile.gpu.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, tile.packed, gl.STATIC_DRAW);
      for (const [loc, n, offset] of [
        [0, 3, 0],
        [1, 4, 12],
        [2, 1, 28],
      ]) {
        gl.enableVertexAttribArray(loc);
        gl.vertexAttribPointer(loc, n, gl.FLOAT, false, 40, offset);
        gl.vertexAttribDivisor(loc, 1);
      }
      gl.bindVertexArray(null);
    }
    update(tile, offset, data) {
      if (!tile.gpu) return;
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, tile.gpu.buffer);
      gl.bufferSubData(gl.ARRAY_BUFFER, offset, data);
    }
    image(slot, bitmap) {
      const gl = this.gl;
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
      gl.texSubImage2D(
        gl.TEXTURE_2D,
        0,
        (slot % COLS) * CELL,
        Math.floor(slot / COLS) * CELL,
        gl.RGBA,
        gl.UNSIGNED_BYTE,
        bitmap,
      );
    }
    render(tiles, cam) {
      const gl = this.gl,
        dpr = Math.min(root.devicePixelRatio || 1, 2);
      const w = Math.round(cam.w * dpr),
        h = Math.round(cam.h * dpr);
      if (this.canvas.width !== w) this.canvas.width = w;
      if (this.canvas.height !== h) this.canvas.height = h;
      gl.viewport(0, 0, w, h);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(this.program);
      const u = uniforms(cam);
      gl.uniform4fv(this.a, u.subarray(0, 4));
      gl.uniform4fv(this.b, u.subarray(4));
      gl.activeTexture(gl.TEXTURE0);
      gl.bindTexture(gl.TEXTURE_2D, this.texture);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(
        gl.SRC_ALPHA,
        gl.ONE_MINUS_SRC_ALPHA,
        gl.ONE,
        gl.ONE_MINUS_SRC_ALPHA,
      );
      for (const tile of tiles)
        if (tile.gpu) {
          gl.bindVertexArray(tile.gpu.vao);
          gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, tile.count);
        }
      gl.bindVertexArray(null);
    }
    remove(tile) {
      if (tile.gpu) {
        this.gl.deleteBuffer(tile.gpu.buffer);
        this.gl.deleteVertexArray(tile.gpu.vao);
        tile.gpu = null;
      }
    }
    dispose() {
      this.canvas.removeEventListener("webglcontextlost", this.lost);
      this.gl.deleteTexture(this.texture);
      this.gl.deleteProgram(this.program);
    }
  }
  class GPURenderer {
    async init(canvas, onFailure) {
      this.canvas = canvas;
      this.adapter = await root.navigator.gpu.requestAdapter({
        powerPreference: "low-power",
      });
      if (!this.adapter) throw Error("Map graphics unavailable");
      const d = (this.device = await this.adapter.requestDevice());
      this.context = canvas.getContext("webgpu");
      if (!this.context) throw Error("Map graphics unavailable");
      this.format = root.navigator.gpu.getPreferredCanvasFormat();
      this.context.configure({
        device: d,
        format: this.format,
        alphaMode: "premultiplied",
      });
      this.uniform = d.createBuffer({
        size: 32,
        usage: root.GPUBufferUsage.UNIFORM | root.GPUBufferUsage.COPY_DST,
      });
      this.texture = d.createTexture({
        size: [SIZE, SIZE],
        format: "rgba8unorm",
        usage:
          root.GPUTextureUsage.TEXTURE_BINDING |
          root.GPUTextureUsage.COPY_DST |
          root.GPUTextureUsage.RENDER_ATTACHMENT,
      });
      const module = d.createShaderModule({ code: WGSL });
      const info = await module.getCompilationInfo();
      if (info.messages.some((m) => m.type === "error"))
        throw Error(info.messages.map((m) => m.message).join("\n"));
      this.pipeline = await d.createRenderPipelineAsync({
        layout: "auto",
        vertex: {
          module,
          entryPoint: "vs",
          buffers: [
            {
              arrayStride: 40,
              stepMode: "instance",
              attributes: [
                { shaderLocation: 0, offset: 0, format: "float32x3" },
                { shaderLocation: 1, offset: 12, format: "float32x4" },
                { shaderLocation: 2, offset: 28, format: "float32" },
              ],
            },
          ],
        },
        fragment: {
          module,
          entryPoint: "fs",
          targets: [
            {
              format: this.format,
              blend: {
                color: {
                  srcFactor: "src-alpha",
                  dstFactor: "one-minus-src-alpha",
                },
                alpha: { srcFactor: "one", dstFactor: "one-minus-src-alpha" },
              },
            },
          ],
        },
        primitive: { topology: "triangle-list" },
      });
      this.bind = d.createBindGroup({
        layout: this.pipeline.getBindGroupLayout(0),
        entries: [
          { binding: 0, resource: { buffer: this.uniform } },
          { binding: 1, resource: this.texture.createView() },
          {
            binding: 2,
            resource: d.createSampler({
              minFilter: "linear",
              magFilter: "linear",
            }),
          },
        ],
      });
      d.lost.then((e) => {
        if (!this.disposed) onFailure(Error(e.message || "Map graphics reset"));
      });
      d.addEventListener("uncapturederror", (e) => {
        if (!this.disposed) onFailure(e.error);
      });
      return this;
    }
    upload(tile) {
      tile.gpu = {
        buffer: this.device.createBuffer({
          size: Math.max(40, tile.packed.byteLength),
          usage: root.GPUBufferUsage.VERTEX | root.GPUBufferUsage.COPY_DST,
        }),
      };
      this.device.queue.writeBuffer(tile.gpu.buffer, 0, tile.packed);
    }
    update(tile, offset, data) {
      if (tile.gpu)
        this.device.queue.writeBuffer(tile.gpu.buffer, offset, data);
    }
    image(slot, bitmap) {
      this.device.queue.copyExternalImageToTexture(
        { source: bitmap },
        {
          texture: this.texture,
          origin: [(slot % COLS) * CELL, Math.floor(slot / COLS) * CELL],
        },
        [CELL, CELL],
      );
    }
    render(tiles, cam) {
      const dpr = Math.min(root.devicePixelRatio || 1, 2),
        w = Math.round(cam.w * dpr),
        h = Math.round(cam.h * dpr);
      if (this.canvas.width !== w) this.canvas.width = w;
      if (this.canvas.height !== h) this.canvas.height = h;
      this.device.queue.writeBuffer(this.uniform, 0, uniforms(cam));
      const encoder = this.device.createCommandEncoder(),
        pass = encoder.beginRenderPass({
          colorAttachments: [
            {
              view: this.context.getCurrentTexture().createView(),
              clearValue: { r: 0, g: 0, b: 0, a: 0 },
              loadOp: "clear",
              storeOp: "store",
            },
          ],
        });
      pass.setPipeline(this.pipeline);
      pass.setBindGroup(0, this.bind);
      for (const tile of tiles)
        if (tile.gpu) {
          pass.setVertexBuffer(0, tile.gpu.buffer);
          pass.draw(6, tile.count);
        }
      pass.end();
      this.device.queue.submit([encoder.finish()]);
    }
    remove(tile) {
      tile.gpu?.buffer.destroy();
      tile.gpu = null;
    }
    dispose() {
      this.disposed = true;
      this.uniform?.destroy();
      this.texture?.destroy();
      this.context?.unconfigure();
      this.device?.destroy();
    }
  }
  async function create(
    canvas,
    { webglOnly = false, onFailure = () => {} } = {},
  ) {
    if (!webglOnly && root.navigator?.gpu) {
      const renderer = new GPURenderer();
      try {
        return await renderer.init(canvas, onFailure);
      } catch (e) {
        renderer.dispose();
        /* WebGPU context acquisition prevents WebGL on this canvas. */ if (
          canvas.getContext("webgl2")
        )
          return new GLRenderer(canvas, onFailure);
        const next = canvas.cloneNode(false);
        canvas.replaceWith(next);
        const gl = new GLRenderer(next, onFailure);
        gl.replacementCanvas = next;
        return gl;
      }
    }
    return new GLRenderer(canvas, onFailure);
  }
  class Atlas {
    constructor(
      renderer,
      changed,
      { maxSlots = 256, maxQueue = 48, concurrency = 2 } = {},
    ) {
      this.renderer = renderer;
      this.changed = changed;
      this.maxSlots = Math.min(maxSlots, 256);
      this.maxQueue = maxQueue;
      this.concurrency = concurrency;
      this.entries = new Map();
      this.queue = [];
      this.active = 0;
      this.slots = 0;
      this.disposed = false;
      this.controllers = new Set();
      this.pins = new Set();
      this.bindings = new Map();
      this.freeSlots = [];
    }
    setPins(ids) {
      this.pins = new Set([...ids].slice(0, this.maxSlots));
    }
    bind(id, tile, offset) {
      const e = this.entries.get(id);
      if (!e?.ready) return null;
      let byTile = this.bindings.get(id);
      if (!byTile) {
        byTile = new Map();
        this.bindings.set(id, byTile);
      }
      let offsets = byTile.get(tile);
      if (!offsets) {
        offsets = new Set();
        byTile.set(tile, offsets);
      }
      offsets.add(offset);
      return e.slot;
    }
    releaseTile(tile) {
      for (const byTile of this.bindings.values()) byTile.delete(tile);
    }
    evict(id) {
      const e = this.entries.get(id);
      if (!e) return;
      e.controller?.abort();
      this.queue = this.queue.filter((item) => item !== e);
      const byTile = this.bindings.get(id);
      if (byTile)
        for (const [tile, offsets] of byTile)
          for (const offset of offsets) {
            tile.packed[offset] = -1;
            this.renderer.update(
              tile,
              offset * 4,
              tile.packed.subarray(offset, offset + 1),
            );
          }
      this.bindings.delete(id);
      this.entries.delete(id);
      return e.slot;
    }
    pause(value = true) {
      this.paused = value;
      if (value) {
        for (const [id, e] of [...this.entries])
          if (!e.ready) {
            const slot = this.evict(id);
            this.freeSlots.push(slot);
          }
        this.queue.length = 0;
      } else this.pump();
    }
    request(id) {
      if (this.paused) return null;
      let e = this.entries.get(id);
      if (e) {
        this.entries.delete(id);
        this.entries.set(id, e);
        return e.ready ? e.slot : null;
      }
      if (this.disposed || this.queue.length >= this.maxQueue) return null;
      let slot;
      if (this.freeSlots.length) slot = this.freeSlots.pop();
      else if (this.slots < this.maxSlots) slot = this.slots++;
      else {
        const victim = [...this.entries.keys()].find(
          (key) => !this.pins.has(key),
        );
        if (victim == null) return null;
        slot = this.evict(victim);
      }
      e = { id, slot, ready: false };
      this.entries.set(id, e);
      this.queue.push(e);
      this.pump();
      return null;
    }
    pump() {
      while (
        !this.disposed &&
        !this.paused &&
        this.active < this.concurrency &&
        this.queue.length
      ) {
        const e = this.queue.shift(),
          ctl = new AbortController();
        this.controllers.add(ctl);
        e.controller = ctl;
        this.active++;
        (async () => {
          let bitmap;
          try {
            const response = await root.fetch("/img/" + e.id, {
              signal: ctl.signal,
            });
            if (!response.ok) throw Error("Picture unavailable");
            const blob = await response.blob();
            if (blob.size > 2 * 1024 * 1024) throw Error("Picture too large");
            bitmap = await root.createImageBitmap(blob, {
              resizeWidth: CELL,
              resizeHeight: CELL,
              resizeQuality: "low",
            });
            if (
              !this.disposed &&
              !ctl.signal.aborted &&
              this.entries.get(e.id) === e
            ) {
              this.renderer.image(e.slot, bitmap);
              e.ready = true;
              this.changed();
            }
          } catch (_) {
          } finally {
            bitmap?.close();
            this.controllers.delete(ctl);
            this.active--;
            this.pump();
          }
        })();
      }
    }
    dispose() {
      this.disposed = true;
      for (const ctl of this.controllers) ctl.abort();
      this.queue.length = 0;
      this.entries.clear();
      this.bindings.clear();
      this.pins.clear();
    }
  }
  const api = {
    create,
    Atlas,
    GLRenderer,
    GPURenderer,
    GLSL_VERTEX,
    GLSL_FRAGMENT,
    WGSL,
    uniforms,
  };
  root.MapUniverseGPU = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
