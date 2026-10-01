/* Universe lifecycle is isolated from the stable, paged map fallback. */
(function (root) {
  "use strict";
  const Base =
    root.MapModel ||
    (typeof require === "function" ? require("./map-model.js") : null);
  const Core =
    root.MapCore ||
    (typeof require === "function" ? require("./map-core.js") : null);
  class UniverseMapModel extends Base.MapModel {
    constructor(o = {}) {
      super(o);
      this.container = o.container;
      this.engineFactory =
        o.engineFactory || ((opts) => new root.MapUniverse.Universe(opts));
      this.universe = null;
      this.universeChecked = false;
      this.universeStart = null;
      this.positions = new Map();
    }
    async load(opt = {}) {
      if (this.paused) return;
      if (!this.universeChecked) {
        if (!this.universeStart) this.universeStart = this.startUniverse();
        await this.universeStart;
        if (this.paused) return;
        if (!this.universeChecked) return this.load(opt);
      }
      if (this.universe) {
        await this.universe.updateCamera(this.cam);
        return;
      }
      return super.load(opt);
    }
    async startUniverse() {
      this.pending = 1;
      this.setPhase("loading");
      this.emit("busy");
      let checked = false,
        engine = null,
        activated = false,
        initialCamera = null,
        initialZoom = 4;
      try {
        const ticket = this.viewReq.begin();
        const manifest = await this.fetchJson("/api/map/universe/manifest", {
          signal: ticket.signal,
        });
        if (!ticket.live()) return;
        checked = true;
        if (!manifest?.available) return;
        const owner = {
          ...(manifest.owner || {}),
          id: manifest.anchor?.person_id,
          handle: manifest.owner?.handle || manifest.anchor?.handle || "",
          name: manifest.owner?.name || manifest.anchor?.name || "",
          pic: manifest.anchor?.person_id
            ? "/img/" + manifest.anchor.person_id
            : null,
          x: 0.5,
          y: 0.5,
        };
        engine = this.engineFactory({
          manifest,
          fetchJson: this.fetchJson,
          onChange: () => {
            this.emit("scene");
          },
          onError: (e) => {
            this.error = e?.message || "Could not read the map.";
            this.stale = "error";
            this.emit("stale");
          },
        });
        this.startingEngine = engine;
        // Open a fresh large map on the inner neighborhood. Existing camera
        // choices, including a pending zoom or flight, remain in place.
        if (
          !this.loaded &&
          !this.flight &&
          !this.goal &&
          !this.vel &&
          this.cam.cx === 0.5 &&
          this.cam.cy === 0.5 &&
          this.cam.k === 1 &&
          manifest.node_count > 1
        ) {
          initialCamera = this.cam.state();
          const framing = Number(manifest.initial_camera?.radius);
          const universeAPI = root.MapUniverse ||
            (typeof require === "function" ? require("./map-universe.js") : null);
          if (Number.isFinite(framing) && framing > 0 && universeAPI) {
            // Frame a readable neighborhood, independently of the outer graph extent.
            initialZoom = Math.max(1, Math.min(16384,
              universeAPI.transformFor(manifest).span / (framing * 2)));
          }
          this.cam.maxK = 16384;
          this.cam.set(0.5, 0.5, initialZoom);
        }
        const mounted = await engine.mount(this.container, {
          camera: this.cam,
          owner,
        });
        if (!ticket.live()) {
          checked = false;
          return;
        }
        if (!mounted) return;
        this.universe = engine;
        activated = true;
        this.cam.maxK = 16384;
        if (this.startingEngine === engine) this.startingEngine = null;
        this.loaded = { universe: true };
        this.scene.clear();
        this.cohort = false;
        this.fallback = false;
        this.world = {
          layout: "universe",
          me: owner.id != null ? owner : null,
          guides: [],
        };
        this.total = this.worldTotal = manifest.node_count || 0;
        this.shown = 0;
        this.hidden = 0;
        this.rev = manifest.version;
        this.scope = "all";
        this.minFit = "";
        this.status = "";
        this.follow = "all";
        this.viewMode = "network";
        this.size = "followers";
        this.mode = "closeness";
        this.setPhase("ready");
        this.emit("mode");
        this.emit("filters");
        this.emit("scene");
      } catch (e) {
        if (e?.name === "AbortError") return;
        checked =
          !this
            .paused; /* A missing snapshot or GPU keeps the existing map usable. */
      } finally {
        if (engine && !activated) engine.dispose();
        if (
          !activated &&
          initialCamera &&
          this.cam.cx === 0.5 &&
          this.cam.cy === 0.5 &&
          this.cam.k === initialZoom &&
          !this.flight &&
          !this.goal
        ) {
          this.cam.set(initialCamera.cx, initialCamera.cy, initialCamera.k);
        }
        if (this.startingEngine === engine) this.startingEngine = null;
        this.universeChecked = checked && !this.paused;
        this.universeStart = null;
        this.pending = 0;
        this.emit("busy");
      }
    }
    moved() {
      if (!this.universe) return super.moved();
      if (this.paused) return;
      this.schedule();
      this.emit("camera");
    }
    needsLoad() {
      return !this.universe && super.needsLoad();
    }
    pause(on) {
      super.pause(on);
      this.universe?.pause?.(on);
      if (on) this.startingEngine?.pause?.(true);
    }
    dispose() {
      this.pause(true);
      this.universe?.dispose();
      this.universe = null;
    }
    setViewMode(id) {
      if (this.universe) return;
      super.setViewMode(id);
    }
    setSizeEncoding(id) {
      if (this.universe) return;
      super.setSizeEncoding(id);
    }
    setDensity(n) {
      if (this.universe) return;
      super.setDensity(n);
    }
    setFilters(f) {
      if (this.universe) return;
      super.setFilters(f);
    }
    setQuery(q) {
      if (this.universe) return;
      super.setQuery(q);
    }
    browse(d) {
      if (this.universe) return;
      return super.browse(d);
    }
    repack() {
      if (this.universe) return;
      super.repack();
    }
    async locateUniverse(id, signal) {
      const known = this.positions.get(String(id));
      if (known) return known;
      const person = await this.universe.locate(id, { signal });
      if (person) {
        this.remember(person);
        return person;
      }
      return null;
    }
    remember(person) {
      const key = String(person.id);
      this.positions.delete(key);
      this.positions.set(key, person);
      if (this.positions.size > 4096)
        this.positions.delete(this.positions.keys().next().value);
    }
    select(n) {
      if (!this.universe) return super.select(n);
      if (!n) return this.deselect();
      this.remember(n);
      super.select(n);
    }
    async loadEdges(n) {
      if (!this.universe) return super.loadEdges(n);
      const ticket = this.edgeReq.begin();
      this.edges = { id: n.id, state: "loading", lines: [], seeds: [] };
      this.emit("edges");
      try {
        const raw = await this.fetchJson(
          "/api/map/universe/edges?ids=" +
            encodeURIComponent(n.id) +
            "&version=" +
            encodeURIComponent(this.rev),
          { signal: ticket.signal },
        );
        const links = (raw.edges || raw.links || []).filter(
          (e) =>
            String(e.source ?? e.from ?? e.a) === String(n.id) ||
            String(e.target ?? e.to ?? e.b) === String(n.id),
        );
        const ids = [
          ...new Set(
            links
              .flatMap((e) => [
                e.source ?? e.from ?? e.a,
                e.target ?? e.to ?? e.b,
              ])
              .filter((id) => id != null)
              .map(String),
          ),
        ];
        const nodes = [];
        for (
          let offset = 0;
          offset < ids.length && ticket.live();
          offset += 200
        ) {
          const reply = await this.fetchJson(
            "/api/map/universe/locate?ids=" +
              ids
                .slice(offset, offset + 200)
                .map(encodeURIComponent)
                .join(",") +
              "&version=" +
              encodeURIComponent(this.rev),
            { signal: ticket.signal },
          );
          for (const raw of reply.nodes || []) {
            const position = this.universe.normalize(raw.x, raw.y);
            const radiusPosition = this.universe.normalize(
              raw.x + (raw.r || 0),
              raw.y,
            );
            const person = {
              ...raw,
              ...position,
              portraitRadius: Math.abs(radiusPosition.x - position.x),
            };
            this.remember(person);
            nodes.push(person);
          }
        }

        if (!ticket.live() || String(this.selected?.id) !== String(n.id))
          return;
        // readEdges accepts only the resolved universe nodes: legacy reply coordinates never enter this scene.
        const reply = { ...raw, nodes, edges: links };
        this.edges = this.readEdges(n, reply);
        this.emit("edges");
      } catch (e) {
        if (!ticket.live() || e?.name === "AbortError") return;
        this.edges = { id: n.id, state: "error", lines: [], seeds: [] };
        this.emit("edges");
      }
    }
    async searchPeople(q) {
      if (!this.universe) return super.searchPeople(q);
      const ticket = this.searchReq.begin();
      q = q.trim();
      if (!q) return [];
      const reply = await this.fetchJson(
        "/api/map/universe/search?q=" +
          encodeURIComponent(q) +
          "&version=" +
          encodeURIComponent(this.rev),
        { signal: ticket.signal },
      );
      return ticket.live() ? reply.results || [] : null;
    }
    async goTo(n) {
      if (!this.universe) return super.goTo(n);
      const ticket = this.locateReq.begin();
      try {
        const person = await this.locateUniverse(n.id, ticket.signal);
        if (!ticket.live() || !person) return;
        this.select(person);
        const base = this.cam.scale / this.cam.k,
          r = person.portraitRadius || 0.001;
        this.flyTo(
          {
            cx: person.x,
            cy: person.y,
            k: Core.clamp(
              Math.max(this.cam.k, 18 / (r * base)),
              Core.K_MIN,
              this.cam.maxK,
            ),
          },
          450,
        );
      } catch (e) {
        if (ticket.live() && e?.name !== "AbortError") {
          this.error = e.message;
          this.emit("stale");
        }
      }
    }
  }
  root.UniverseMapModel = UniverseMapModel;
  if (typeof module !== "undefined") module.exports = { UniverseMapModel };
})(typeof window === "undefined" ? globalThis : window);
