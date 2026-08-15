# OpenPlaces PH architecture

OpenPlaces PH is built around one practical constraint:

> **A nationwide place-reconciliation pipeline should finish reliably on a modest 8 GB laptop without requiring the Philippines to fit in RAM.**

That constraint drives nearly every implementation choice: tiling, Parquet checkpoints, conservative parallelism, out-of-core DuckDB operations, narrow national sorts, and compact clustering state.

## 1. Pipeline at a glance

```text
Geofabrik OSM PBF ----------> filter / normalize / partition ---\
                                                              \
Overture Places ------------> normalize / partition ------------+--> pairwise match shards
                                                                |          |
Foursquare OS Places -------> normalize / partition ------------/          v
                                                                    accepted links
                                                                          |
                                                                          v
                                                                    narrow ranking
                                                                          |
                                                                          v
                                                               constrained union-find
                                                                          |
                                                                          v
                                                              canonical GeoParquet points
```

There are deliberately two notions of “done”:

- **local durable work**: source tiles, match shards, union-find snapshots;
- **public output**: observations, accepted match edges, canonical points, and summary.

A crash should cost only the current durable unit, not the entire country.

---

## 2. Memory policy

Default DuckDB limits are small on purpose:

- worker connection: **512 MB**;
- national/final connection: **1 GB**;
- DuckDB threads per connection: **1**;
- Overture workers: **1**;
- Foursquare workers: **2**;
- match workers: **1**.

DuckDB can spill joins and sorts to `--temp-dir`. The code never materializes a national Pandas or GeoPandas table.

### Why not use all eight logical CPU threads?

On an old laptop, CPU, RAM, disk bandwidth, filesystem cache, antivirus, and thermal throttling are coupled. More workers can make every worker slower while also increasing out-of-memory and paging risk. The defaults therefore leave concurrency to the stages that benefit most from overlapping remote latency.

---

## 3. Source acquisition

### OpenStreetMap

The national Geofabrik PBF is downloaded once with HTTP byte-range resume. Osmium performs the large PBF filtering outside Python, keeping only named objects carrying POI-relevant keys.

The filtered geometry is exported to GeoJSONSeq and converted to partitioned Parquet. Because the product is intentionally a **point registry**, lines and polygons are reduced using `ST_PointOnSurface`. Unlike a centroid, this representative point remains on the original geometry.

### Overture Maps

The code resolves one Overture release and pins it in cache metadata. Every resumed Overture tile therefore comes from the same snapshot.

A country land geometry from that same release serves two purposes:

1. remove 1-degree tiles that are entirely ocean;
2. reject clearly offshore place points.

Strict point-in-polygon containment would affect Overture only, because Foursquare uses `country='PH'` and OSM comes from Geofabrik. That asymmetry would systematically depress source counts on reclaimed land, ports, piers, and small/coastal geometries. The Overture test therefore uses a small tolerance.

All Overture boundary operations explicitly use **`OGC:CRS84`**, where X is longitude and Y is latitude. This avoids silent CRS ambiguity after DuckDB 1.5 introduced CRS-aware geometry types.

### Foursquare

Foursquare OS Places is queried through authenticated Hugging Face Parquet. `country='PH'` and coordinate bounds are both applied, so only relevant row groups are transferred/materialized.

Heavy acquisition libraries are imported lazily. Merely asking for `openplaces --help` or `--status` does not import Overture and Hugging Face client stacks.

---

## 4. Durable work units

| Stage | Durable unit | Normal loss after interruption |
|---|---|---|
| OSM national download | `.part` byte range | only uncommitted bytes |
| OSM local preparation | local stage | current local stage |
| Overture | 1-degree tile | current tile |
| Foursquare | 1-degree tile | current tile |
| Pairwise matching | 0.25-degree tile × source pair | current shard |
| Union-find | transactional snapshot | row groups after last snapshot |

Output files are written to temporary `.part` paths and renamed only after successful completion.

---

## 5. Matching

Matching is pairwise across sources:

```text
FSQ <-> Overture
FSQ <-> OSM
Overture <-> OSM
```

For one 0.25-degree core tile:

1. the left source is read only inside the core;
2. the right source is read from the core plus a distance halo;
3. each observation gets a numeric grid cell;
4. only the 3×3 neighborhood is joined;
5. cheap latitude/longitude separation bounds reject impossible pairs;
6. Haversine distance is computed for the survivors;
7. pairs beyond `max_distance_m` are discarded;
8. normalized name and sorted-token strings are scored with Jaro-Winkler;
9. distance-dependent thresholds decide whether the edge exists;
10. category similarity contributes only 2% of the final ranking score.

### 5.1 Blocking cell size is derived

A 3×3 neighbor join only guarantees coverage to one cell width. A hard-coded cell would therefore make some `--max-distance` values incorrect.

`MatchConfig.grid_degrees` is derived from the requested radius using a conservative longitude scale at the northern edge of the Philippines plus a small safety factor. The tests sweep bearings, sub-cell positions, several distances, and Philippine latitudes to verify that an in-range pair never lands more than one grid cell apart.

### 5.2 Cheap bounds before Haversine

Grid neighbors form a square, while the desired search region is a circle. Many joined pairs can be rejected using only:

```text
abs(delta_lat) <= safe_lat_bound
abs(delta_lon) <= safe_lon_bound
```

before evaluating trigonometric functions. This is a strict superset of the search circle, so it improves performance without changing recall.

### 5.3 One acceptance calibration

The model uses this default piecewise rule:

| Distance interval | Minimum Jaro-Winkler name score |
|---|---:|
| 0–20 m | 0.70 |
| 20–50 m | 0.82 |
| 50–90 m | 0.90 |
| >90 m | 0.94 |

The configured radius merely truncates the final interval. A 60 m run therefore ends with `50–60 m -> 0.90`; it does not import the 0.94 threshold from the >90 m interval.

`acceptance_bands()` is the single source of truth. Both `accept_pair()` and the SQL `CASE` are generated from it and tested against each other.

Generic/short names have a separate stricter rule, but even that rule cannot exceed the global `max_distance_m`.

---

## 6. Finalization without a huge national sort

Accepted edge shards contain wide fields: source IDs, scores, distances, provenance-related flags, and strings.

Clustering only needs a ranked `(left_id, right_id)` pair. Finalization therefore splits the work:

```text
edge shards
   |
   v
edge_ids.parquet      # wide, unsorted, dense observation IDs attached
   |
   +-------------------------------> later audit/output join
   |
   v
ranked_pairs.parquet  # only two int64 columns, globally sorted by edge strength
   |
   v
union-find
```

The expensive national sort carries only the pair. Tie-breaking still uses stable upstream source IDs before those strings are discarded, keeping clustering deterministic when dense row IDs are rebuilt.

### Edge-resolution invariant

Every accepted source edge must resolve to exactly one observation on each side. After `edge_ids.parquet` is written, metadata row counts are compared with the sum of accepted source-edge rows. A mismatch means a source ID was missing or duplicated and finalization aborts instead of silently changing the graph.

---

## 7. Dense observation IDs without a national window

A single filtered staging Parquet file is written first. It is then re-read with DuckDB's `file_row_number=true` to attach dense IDs `0..n-1`.

This deliberately avoids `row_number() OVER ()`, which can route the national table through a window operator and increase memory/spill pressure.

---

## 8. Constrained union-find

Edges are processed strongest-first. A union is rejected if the two components already contain the same source.

The source mask uses three bits:

```text
1 = Foursquare
2 = Overture
4 = OSM
```

If:

```text
mask[root_a] & mask[root_b] != 0
```

then the merge would create a canonical entity with duplicate observations from at least one source and is rejected.

### 8.1 The state is genuinely compact in RAM

The national implementation uses:

- `array('I')` parent: 4 bytes/observation;
- `bytearray` rank: 1 byte/observation;
- `bytearray` source mask: 1 byte/observation.

So the base working state is about **6 bytes per observation**.

A Python `list[int]` was deliberately rejected. Although it can be fast for scalar access, each list slot points to a Python integer object and the real memory footprint becomes tens of bytes per observation.

The code refuses to run if the platform's `array('I')` is not four bytes or if the observation count reaches `2^32`, rather than silently widening the representation and violating the memory contract.

### 8.2 Transactional resume

A committed checkpoint contains:

```text
parent:uint32
rank:uint8
mask:uint8
completed_row_group:int64
input signature
```

It is written to a temporary file, fsynced, then atomically promoted. A crash cannot mutate the previous committed checkpoint. Replaying later ranked-edge row groups is deterministic.

### 8.3 Final path compression

The compact parent buffer is exposed to NumPy as a zero-copy `uint32` view. Pointer doubling compresses the forest with one temporary `uint32` vector rather than converting the entire national parent array to `int64`.

---

## 9. Transitivity is instrumented

The algorithm does not require triangle closure.

A triple can be formed by:

```text
FSQ <-> Overture
Overture <-> OSM
```

without an accepted FSQ↔OSM edge.

The canonical output therefore includes:

- `completed_transitively`: true when a three-source cluster has fewer internal accepted links than its three possible pairs;
- `cluster_max_pair_distance_m`: greatest distance between any two observations in that cluster.

Strict triangle closure is intentionally not imposed yet. The diagnostics let empirical runs show how much a stricter future mode would affect recall.

---

## 10. Canonical record construction

For each cluster:

- coordinates are the median longitude and latitude;
- name/category use explicit source priority;
- every source's original ID/name/category is retained;
- Overture provenance is retained;
- source-specific license fields are retained;
- `source_count` records source-layer coverage;
- `known_independent_source_count` discounts the visible Overture-from-Foursquare case;
- transitivity diagnostics are retained.

The geometry is written as a CRS-aware point:

```text
OGC:CRS84
```

Before the temporary canonical file is promoted, PyArrow checks for the GeoParquet `geo` metadata key. If it is absent, finalization fails loudly.

---

## 11. Licensing architecture

The repository software is MIT-licensed. Generated data inherit obligations from their upstream records and are not automatically MIT data.

Normalized observations therefore carry `upstream_license`; canonical rows expose source-specific license fields. Unknown or mixed Overture provider provenance is marked conservatively rather than guessed.

See [`../DATA_LICENSES.md`](../DATA_LICENSES.md).
