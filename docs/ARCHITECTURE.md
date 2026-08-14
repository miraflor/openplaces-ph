# OpenPlaces PH architecture

OpenPlaces PH is designed around a simple constraint: **a nationwide data pipeline should finish reliably on modest hardware without requiring the entire Philippines to fit in RAM.**

The implementation is therefore disk-first, tiled, checkpointed, and intentionally conservative about parallelism.

## Pipeline

```text
Geofabrik OSM PBF ---------> normalize/partition ----\
                                                    \
Overture Places -----------> normalize/partition -----+--> pairwise match shards
                                                      |         |
Foursquare OS Places ------> normalize/partition -----/         v
                                                           ranked links
                                                               |
                                                               v
                                                     constrained clustering
                                                               |
                                                               v
                                                     canonical GeoParquet points
```

## Durable work units

| Stage | Durable unit | Typical loss after interruption |
|---|---|---|
| Geofabrik OSM download | HTTP byte-range `.part` file | only bytes not yet committed |
| OSM filter/export | one local stage | current local stage |
| Overture acquisition | 1-degree tile | current tile |
| Foursquare acquisition | 1-degree tile | current tile |
| Pairwise matching | 0.25-degree tile x source pair | current match shard |
| Greedy clustering | ranked-edge row group | current row group |

Completed files are written to temporary `.part` paths and promoted only after successful completion.

## Memory policy

The defaults deliberately reserve most machine memory for Windows, filesystem cache, antivirus, and other applications:

- DuckDB source/match worker: **512 MB**
- DuckDB national/final stage: **1 GB**
- DuckDB threads per connection: **1**
- Overture workers: **1**
- Foursquare workers: **2**
- match workers: **1**

DuckDB may spill large sorts and joins to `--temp-dir`. An SSD is preferred.

The pipeline avoids national Pandas and GeoPandas materialization. Python mainly orchestrates durable units; DuckDB, Osmium, Arrow, and Parquet do the heavy work.

## Source acquisition

### OpenStreetMap

The Philippines `.osm.pbf` is downloaded once from Geofabrik. Osmium filters named objects carrying POI-relevant keys and exports only the required attributes. The result is partitioned into source tiles.

OSM polygons and lines are reduced to point coordinates because OpenPlaces PH's canonical product is intentionally a point layer.

### Overture

The Overture client streams one bounded tile at a time from a pinned release. A Philippine `division_area` land polygon from that same release is cached and used to remove ocean-only source tiles and clip returned Places to Philippine land.

Overture's raw `sources` provenance is preserved. Provider-level license metadata is inferred conservatively from that provenance.

### Foursquare

DuckDB queries the authenticated Hugging Face Parquet dataset remotely. Both `country='PH'` and geographic bounds are applied so the pipeline does not materialize the global Foursquare dataset.

## Matching strategy

Matching is source-pair specific:

```text
Foursquare <-> Overture
Foursquare <-> OSM
Overture   <-> OSM
```

For each 0.25-degree core tile:

1. read only touching 1-degree source files;
2. extend the comparison side with a small spatial halo;
3. use a coarse numeric coordinate grid to generate nearby candidates;
4. calculate exact Haversine distance;
5. discard candidates beyond the configured maximum distance;
6. compare normalized name and sorted-name tokens with Jaro-Winkler similarity;
7. use category similarity as weak supporting evidence; and
8. apply conservative distance-dependent acceptance thresholds.

Generic labels such as `ATM`, `Bank`, or `Clinic` require near-exact agreement at very short distances.

## Constrained clustering

Accepted candidate links are sorted strongest-first. A compact union-find data structure greedily joins observations subject to one crucial constraint:

> A canonical cluster may contain at most one Foursquare observation, one Overture observation, and one OSM observation.

This prevents transitive matching from collapsing multiple same-source branches in dense locations such as malls.

The union-find uses compact NumPy arrays and is transactionally snapshotted between ranked-edge row groups. If the process stops, the last completed snapshot remains valid.

## Canonical record construction

For each final cluster:

- canonical coordinates are the median longitude and latitude;
- canonical name/category use explicit source priority;
- each original source ID/name/category is retained;
- Overture provenance is retained;
- source-specific license metadata is retained;
- `source_count` records observed source-layer coverage; and
- `known_independent_source_count` discounts the visible Foursquare-inside-Overture provenance case.

Evidence tiers (`single`, `double`, `triple`) describe source coverage only. They are not calibrated probabilities.

## Licensing architecture

OpenPlaces PH keeps software licensing and data licensing separate.

The source code is MIT-licensed. Normalized observations carry an `upstream_license` field, and canonical records expose per-source license fields. Overture's provider-dependent licensing is inferred from its preserved source provenance, with unknown or mixed future cases marked `UNKNOWN` or `MIXED/REVIEW` rather than guessed.

See `DATA_LICENSES.md` for release guidance.
