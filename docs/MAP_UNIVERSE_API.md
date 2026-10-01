# Offline bubble universe

This path complements the existing compact map. It never builds in a request, never modifies the source database, and never controls collection.

Build against an isolated database with `python3 server/map_universe.py --db /absolute/path/fixture.sqlite --anchor owner_handle --traversal undirected --edge-policy all`. Default traversal is outgoing. Default policy `captured` accepts explicit `universe_edges` plus job-target-bound active observations. `current_handle` explicitly enables current_edges resolved at build time. `exports` enables historical export edges with local IDs frozen at import. `all` combines these sources. These are recorded follow chains, not friendship or introduction claims. Job target IDs are read at build time; the legacy schema does not guarantee immutable capture-time source identity.

**Imports and profile/link changes do not update an existing universe. Rebuild it offline to include them. No request starts an automatic build.**

Builds atomically publish `<db>.universe/current.json`, pointing to an immutable version directory. Old versions remain readable for in-flight requests. The manifest compares a cheap source revision/import marker and database/WAL file stamps. This conservatively detects changes, including unrelated workspace writes; no_change_detected is a hint, not a content hash. Older snapshots lacking a source stamp report freshness unknown and expose their build completion time as the legacy source timestamp. Failed builds do not replace the current pointer. Snapshots need rebuilding after source changes. Metadata lives in indexed `index.sqlite`, separate from CSR topology and binary geometry. CSR has little-endian node IDs uint32, offsets uint64, and neighbor ordinals uint32. It deduplicates directed neighbors; explicit traversal can reverse or symmetrize those edges. Evidence retains its original direction and provenance regardless of traversal.

BFS computes shortest recorded path length. Unknown/no-recorded-path uses uint16 `65535`, lives in a distinct outer band, and is never presented as an actual hop count. Communities are weak connected components labeled by their lowest existing person ID. Degree is the number of distinct adjacency neighbors under the chosen traversal. Neither is a fabricated affinity score.

Known hops occupy adjacent disjoint annuli. Hop 1 spans radius 24 to 250, hop 2 spans 250 to 450, and later known bands abut. Person-ID jitter samples radius uniformly by area, filling the space near the owner without artificial blank bands. Bubble radius has logarithmic follower scaling, greater-distance falloff, and a per-annulus density ceiling. Total circle area is bounded to 12.25% of annulus area; this reduces dense solid rings but does not promise zero pairwise collisions. Owner remains at the center. Unknown followers are explicitly flagged.

Tiles partition by hop, then Morton spatial order, and flush at hop boundaries. The owner is the first tile; successive pages expose nearby rings before unknown nodes. Rtree tile bounds support camera selection. No browser topology calculation is required. Every person has one geometry record and can be searched or located, even before its tile loads.

## Endpoints

- `/api/map/universe/manifest`: version, source_snapshot_at, conservative stale/source_changed_since_build hint, auto_rebuild false, total/node_count, anchor/owner, bounds array `[x0,y0,x1,y1]`, span, traversal/evidence semantics, limits, and an initial tile page bounded to 8192 records.
- `/api/map/universe/view?version=...&x0=...&y0=...&x1=...&y1=...&budget=8192`: intersecting tiles, record_count, opaque query-bound next_cursor, complete. Continue with identical camera/version/budget and cursor. Maximum 32 tiles and 65536 records per page. Tiles may include some records outside the camera. All camera records eventually arrive if pagination completes. An offscreen owner tile is not forced into camera results; clients retain owner metadata separately. This is a per-response bound, not a browser resident-node limit or proof that all 5M nodes can be drawn simultaneously. Clients must explicitly report loaded/resident counts and any residency cap.
- `/api/map/universe/tile?version=...&tile=...`: immutable binary response, correct octet-stream MIME, ETag/conditional 304. A tile holds at most 2048 records.
- `/api/map/universe/search?q=prefix&version=...`: indexed case-insensitive handle-prefix search, at most 20 metadata results. It does not globally scan names or substrings.
- `/api/map/universe/locate?ids=1,2&version=...`: at most 200 metadata results with raw world positions, id/person_id, handle/name, followers, hop or null, community, degree, radius, distance_state.
- `/api/map/universe/person?id=...&version=...`: one metadata row plus up to 50 directed outgoing evidence rows and a truncation flag.
- `/api/map/universe/edges?ids=1,2&version=...`: directed incident edges, at most 1000 with truncation flag. Both endpoint orientations are indexed. UI must locate endpoints before drawing them.

## Binary format

All fields little endian. Header is 16 bytes: ASCII `MUV1`, uint32 record count, uint32 stride `32`, uint32 reserved `0`.

Each 32-byte record is `id:uint32, x:float32, y:float32, radius:float32, followers:uint32, hop:uint16, flags:uint16, component:uint32, adjacency_degree:uint32`. Flags bit 0 owner, bit 1 reachable, bit 2 followers known. Unknown hop is `65535`. Floats are raw world units, owner `(0,0)`; manifest span/bounds provide the camera transform.

## Scale verification

The implementation uses O(N+E) typed arrays and on-disk SQLite sorting/indexing. It does not retain Python node or edge objects for the whole graph. The browser streams bounded immutable tiles. This architecture does not by itself prove a 5M build duration, memory ceiling, GPU performance, or a 5M interactive frame rate.

`python3 tests/bench_map_universe.py` builds a 5000-node synthetic fixture. A 5M run requires explicit `--nodes 5000000 --allow-large`, disk reserve, watchdog memory/time limits, and a dedicated empty output directory. It is intentionally not run during normal development or while the user is working. RSS can be unavailable for short builds that finish between watchdog polls; this is reported as null rather than zero.
