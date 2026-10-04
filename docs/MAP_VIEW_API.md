# Map view API

The current Connections UI requests stable pages of people with `cohort=1`.
Zoom and pan only magnify that page; they do not fetch or reveal a different set of people.
Previous/Next, filters and search reach the rest of the database. Browser memory and drawing
are bounded independently of the total database size.

The spatial rectangle API is retained for compatibility and is documented below. The current
UI does not use its zoom-dependent aggregation. The older `/api/map`, `/api/map-overview` and
`/api/connections` endpoints are unchanged. Ten million people remains the storage target;
current physical benchmark limits are in [performance](MAP_VIEW_PERFORMANCE.md).

## Stable pages used by the UI

Request `/api/map/view?mode=closeness&cohort=1&budget=500` with the normal filters.
The UI offers 500, 1,000 or 3,000 people per page. Recorded follows is the default;
Audience overlap is an explicit alternative. Changing arrangement preserves membership and the camera.

- `cohort=1` uses indexed rank/person seeks; compact replies support larger bounded pages.
- `after` is the opaque JSON string returned as `next_cursor`; encode it as a query parameter.
- `next_cursor:null` means there is no next page. The client retains its previous cursors.
- `cohort_reset:true` means the layout revision changed and the response starts at page one.
- `total` and `world_total` count matching people excluding the owner, who is rendered separately.
- `clusters` and `groups` are empty. `hidden` counts matching people on other pages, not bubbles
  waiting to appear on zoom. The spatial cluster-count invariant below applies only to spatial requests.

The client packs each page into deterministic, irregular positions around the owner, ordered
outward by recorded connection evidence. Mutual follows come first, then people recorded following
the owner, then people the owner follows, then explicitly recorded familiarity and source-only evidence.
Lead fit, pipeline status and audience size cannot override these tiers. Distance is an ordering,
not a literal social distance.
The original evidence coordinates remain available on each client node. Every loaded person
is drawn; no collision culling or count chips replace people. Search can replace one member
with the requested person and centre that selection without increasing the loaded page size.

All coordinates are normalized: x and y are in `[0,1]`, origin top-left, y grows downward.
The world always includes `{"w":1,"h":1}`. All current modes also return
`layout:"network_disk"`, `center:{"x":0.5,"y":0.5}` and `radius:0.47`.

## GET /api/map/view

| Param | Meaning |
| --- | --- |
| `mode` | `closeness` (default), `fit`, `seeds` or `status`. Anything else is a 400. |
| `x0,y0,x1,y1` | Requested rectangle. Default `0,0,1,1`. Values outside `[0,1]` are clamped. `x0<x1` and `y0<y1` are required (400 otherwise). |
| `budget` | Maximum individual people. Default 600, clamped to 50..1500. |
| `scope` | `leads` or `all`. Default `all` in `closeness` and `status` modes; `leads` in other modes. `leads` leaves out people marked "not a fit" and people with a known fit below 45. Unread people remain available until qualified. |
| `min_fit` | 0..100. Only people with a qualification fit at or above it. Snapped **down** to 0, 25, 45, 60, 70 or 85; the value used comes back in `filters.min_fit`. Unread people have no fit and never pass. |
| `status` | Comma list of `interested, contacted, talking, spoke_before, client, no, none`, or `all`. Absent means everything in `closeness` and `status` modes; otherwise everything except `no`. |
| `follow` | `all` (default), `following` (owner follows them), `followers` (they follow owner), `mutual`, `not_following` or `unknown`. Negative results require explicit outgoing absence recorded by a complete check; missing outgoing evidence stays unknown, even when an incoming follow is known. |
| `overview` | `1` opts any whole-world mode into spatial overview sampling: up to a quarter of the budget (maximum 120 people) comes from up to 12 occupied spatial sectors. Default `0` keeps exact rank ordering. Search and zoom retain ordinary ranked selection. |
| `q` | Handle or name text. Restricts the population to matches (see Search limits). `total` counts matches, capped at 2,000 (`filters.q_capped`). |

Response:

```json
{
  "rev": 1234,
  "mode": "closeness",
  "ready": true,
  "world": {"w": 1, "h": 1},
  "viewport": {"x0": 0.25, "y0": 0.25, "x1": 0.75, "y1": 0.75},
  "nodes": [
    {"id": 812, "handle": "brand.co", "name": "Brand Co", "x": 0.5123, "y": 0.4871,
     "rank": 0.93, "fit": 81, "status": "client", "cluster": 4, "closeness": 0.71, "lead": true}
  ],
  "clusters": [
    {"id": 90177025, "x": 0.31, "y": 0.62, "count": 1204, "label": "Community around @seedhandle",
     "top_ids": [55, 9012, 771]}
  ],
  "groups": [{"id": 4, "label": "Community around @seedhandle", "x": 0.4, "y": 0.6, "r": 0.05}],
  "total": 88231, "shown": 600, "hidden": 87631,
  "world_total": 9931204,
  "filters": {"scope": "leads", "min_fit": null, "status": null, "q": null, "q_capped": false, "budget": 600},
  "layout": {"ready": true, "rev": 1234, "pending": 0, "built_at": "2026-09-30T12:00:00+00:00",
             "building": false, "progress": null}
}
```

Fields:

- `rev`: integer revision of this mode's layout. It changes whenever anything in a response can
  change (a person moves, is added, gets a new status, or is renamed). Same `rev` and same query means
  the same body.
- `viewport`: the **effective** rectangle. The server snaps the request outward to grid-cell edges
  (cells are `2^-k` wide, chosen so about 8 fit across the longer side), so it is never smaller than
  the request and at most about one eighth larger per side. `total`, `nodes`, `clusters` and `hidden`
  all describe exactly this rectangle. Nodes may therefore sit slightly outside the requested one.
  Nearby pans snap to the same rectangle, so they share one cached body.
- `nodes`: up to `budget` people, sorted by `rank` descending (ties by `id`). They are exactly the
  top-ranked matching people inside `viewport`, except an explicit whole-world `overview=1`
  balances priority people with bounded samples from occupied sectors. Counts remain exact.
  - `id` is the person id (`/api/person/<id>`), `handle` and `name` are current.
  - `rank` 0..1 is importance inside this mode (1 is most important). It only orders people.
  - `fit` 0..100 is the qualification fit, `null` when unread. `status` is the pipeline status
    (`interested`, `contacted`, `talking`, `spoke_before`, `client`, `no`) or `null`.
  - `closeness` 0..1 orders recorded connection evidence. Owner follow directions take precedence
    over explicitly recorded familiarity and source-only evidence. It does not estimate friendship.
  - `connection_kind` preserves the evidence tier independently of lead fit and pipeline status.
    Compact replies carry this category as an additional column.
  - `cluster` is the semantic cluster id for colour (see Modes). `lead` follows `scope=leads`.
  - `source: true` appears only on source (seed) accounts that have a person row.
  - `source_count` is the number of distinct current collected source audiences containing this
    person, including the owner if observed. Both directions of one source count once. This
    bounded field uses the existing maintained source-degree summary.
  - `pic` is the local `/img/<id>` URL only when a cached portrait exists, otherwise `null`.
  - `followed` and `follows_me` preserve the independent observed owner directions.
    `following_evidence` is `observed`, `absent` or `unknown`; `absent` means the last complete
    following check disproved a prior observation. It is not a claim about the live account now.
- `clusters`: bubbles for everyone matching but not shown as a node. Each is one grid cell of the
  viewport crossed with one cluster. `count` is people hidden in it, `x,y` their centroid, `label` the
  cluster name, `top_ids` up to 3 of the highest-ranked hidden people (they appear first when zooming
  in; empty when none rank near the cut). `id` is stable for the same cell and cluster.
  **Invariant: the sum of all `clusters[].count` equals `hidden`.**
- `groups`: the semantic clusters of the mode whose centre lies in `viewport` (label anchors for the
  renderer, independent of `budget`). `r` is a small label-anchor radius; default network
  groups are source/evidence neighborhoods, not circles enclosing every member. `category`
  describes the broad evidence band. The whole disk boundary comes from `world.radius`.
- `total`: people matching the filters inside `viewport`. `shown = len(nodes) <= budget`.
  `hidden = total - shown`. `world_total` is the same count for the whole world.
- `ready:false`: the layout for this mode is not built yet or has an older incompatible schema. `nodes` and `clusters` are empty,
  `layout.building` and `layout.progress` say how far the build is; poll again. Never cached.

Caching: every response carries `ETag` (`"<build>-<rev>-<query hash>"`) and `Cache-Control: no-cache`.
Send `If-None-Match` to get a bodyless 304 while nothing changed. The server also keeps a small cache
keyed on the same thing.

## GET /api/map/edges?ids=1,2,3

Connections for the given people only (up to 200 ids, 400 otherwise). Never all edges.

```json
{"rev": 1234, "ids": [1, 2, 3], "truncated": false,
 "edges": [
   {"kind": "follow", "from": 1, "from_handle": "a", "to": null, "to_handle": "sourceaccount",
    "seed": "sourceaccount", "direction": "followers", "observed_at": "2026-09-01T10:00:00+00:00"},
   {"kind": "overlap", "a": 1, "b": 2, "seeds": ["sourceaccount"], "shared": 1}
 ]}
```

- `follow` is an **observed** directed follow taken from a collected list of a source account.
  `from` follows `to`. In a source's followers list the person follows the source; in its following
  list the source follows the person. `from`/`to` are person ids or `null` when that account has no
  person row; the handle is always given. `seed` and `direction` name the collected list.
- `overlap` says two of the given people were observed in the same collected list(s) (`seeds`, at most
  5 shown, `shared` counts all). It implies no friendship and no follow between them.
- Limits: 50 follow edges per id, 500 overlap edges, 2,000 in total; `truncated` is true when any
  limit cut the result. Only current observations appear.

## GET /api/map/search?q=

Up to 20 people, best match first (exact handle, handle prefix, name and handle contains), with their
place in **every** mode so the map can fly to them:

```json
{"rev": 1234, "q": "brand", "results": [
  {"id": 812, "handle": "brand.co", "name": "Brand Co", "fit": 81, "status": "client",
   "lead": true, "closeness": 0.71, "pending": false,
   "positions": {"closeness": {"x": 0.51, "y": 0.49, "rank": 0.93, "cluster": 4},
                 "fit": {"x": 0.3, "y": 0.3, "rank": 0.9, "cluster": 3},
                 "seeds": {"x": 0.2, "y": 0.7, "rank": 0.8, "cluster": 12},
                 "status": {"x": 0.5, "y": 0.9, "rank": 0.99, "cluster": 5}}}]}
```

`q` needs at least 2 characters (400 otherwise). A person collected moments ago may not have a
position yet: `pending:true`, and modes without a position are missing from `positions`.

### Search limits

Handle prefixes match anywhere in the database (indexed). Handle and name **substring** matches, and
name matches in general, cover the engaged people (any status or fit) and the 65,536 top-ranked
people of the `closeness` layout. This is a stated trade-off of 10-million scale.

## Modes

Each mode has its own precomputed layout and rank. The four layouts are stable: a person keeps their
place until their own data changes.

| Mode | Layout | `cluster` | Rank |
| --- | --- | --- | --- |
| `closeness` | Shared owner-centred network disk. | source/evidence neighborhood | connection evidence, then existing score/fit |
| `fit` | Same exact positions and neighborhoods. | same community ids | existing fit, then connection evidence |
| `seeds` | Same exact positions and neighborhoods. | same community ids | distinct collected sources, then connection evidence |
| `status` | Same exact positions and neighborhoods. | same community ids | workflow status, then existing score |

Changing modes changes importance, visual size/color encoding and which bounded people are shown;
it never relocates a person. Source-count, fit and status fields provide those view encodings.
All modes include the same network boundary and positioned owner context. A person's recorded
connection facts may change their position through incremental maintenance in every mode together.

In every mode a person Michael works with (any status other than `no`) ranks above unread people.

## Freshness and cost

The layout lives beside the database in `<db>.map/` (one SQLite file per mode: an R*Tree over
x, y and rank, a per-person table, and a cell/cluster count pyramid). Requests only read it. A
background worker applies changed people in small batches (`layout.pending` counts what is waiting) and
`python3 server/map_layout.py --db <db> build` rebuilds everything, resumable, outside the request
path. Measurements: `docs/MAP_SCALE.md`.

## Preparing the layout

A fresh installation has no viewport layout. The interface offers the existing map while it is
unprepared. Requests never start a build. On a chosen database, prepare the four layouts explicitly:

```sh
python3 server/map_layout.py --db /absolute/path/to/leads.sqlite build --workers 1
python3 server/map_layout.py --db /absolute/path/to/leads.sqlite status
```

Use a temporary copy for validation. Do not run this against the live database as part of a code
review. Once the server starts with prepared layouts, its maintenance worker applies queued changes.
Missing layouts report `ready:false, layout.building:false`; the UI should call this unprepared.
The edge response also includes `nodes` for positioned endpoints and accepts `mode`.
The view includes `world.me` only when the owner has an existing, positioned person record.

Layouts are derived data. Database and workspace backups preserve the people and recorded evidence,
but do not include the `<db>.map/` directory. After restoring a database, prepare new layouts with
the commands above. The bounded overview remains usable before preparation. Do not copy layout
files from a different database snapshot, because their person IDs and revision may differ.

## Default network arrangement

The owner stays at the centre of one circular whole. Collected source audiences form stable angular
neighborhoods, with smaller evidence groups inside them. Broad radial bands distinguish directly
recorded owner follows or manual relationship context, links through recorded clients or known
sources, shared audiences, and other collected accounts. Radial bands overlap for readable packing;
positions within them do not express a precise friendship distance. Unknown evidence stays explicit.
Recorded follows and shared audiences do not prove friendship or an introduction.

People use actual cached portraits when available. Group counts describe hidden people and individual
size describes existing fit. The optional overview keeps priority people and adds bounded spatial
samples so the outer neighborhoods remain visible. Zoom reveals further ranked people within the
requested region; search locates saved profiles directly. Other modes emphasize fit, source count and workflow status within those same positions.

Layout schema 6 shares geometry across all four modes and stores owner-direction facets alongside status and fit in the precomputed class.
The API uses bounded integer class predicates rather than binding an oversized bitmask to SQLite.
A partial-mode build reuses the current shared geometry plan. Rebuild all four modes to refresh
source anchors together. Older layouts report unprepared and remain untouched by incremental updates; an explicit build
creates the current schema. Interrupted plans from an older schema are not resumed.

The interface keeps the full filtered population separate from the count inside the current viewport. Layout preparation and browser rendering remain separate from collection and qualification.

## Quiet connection lines and selection

The compact portrait map draws a bounded set of faint recorded follow paths behind portraits even
without a selection. Audience overlap does not create a follow path. Selecting a person emphasizes
their recorded directions, draws that portrait above neighbors and enlarges it visibly. Hit testing
prefers a portrait body over another selection's extra click area.
