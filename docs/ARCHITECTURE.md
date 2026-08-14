# Architecture: why this runs on an old laptop

The pipeline is intentionally **disk-first**. It never constructs a national
GeoPandas/Pandas dataframe and never performs a Philippines-wide all-pairs POI
comparison.

## Durable work units

| Stage | Durable unit | Normal loss after interruption |
|---|---|---|
| Geofabrik OSM download | HTTP byte range (`.part`) | only bytes not yet downloaded |
| OSM filter/export | one local stage | current local stage |
| Overture acquisition | 1° tile | current tile |
| Foursquare acquisition | 1° tile | current tile |
| Pairwise matching | 0.25° tile × source pair | current tiny edge shard |
| Greedy clustering | 100k ranked-edge row group | current row group |

Files are normally written to a temporary `*.part` path and promoted with an
atomic rename only after they are complete.

## Memory policy

The target machine has 16 GB installed RAM, but the defaults deliberately use
much less:

- DuckDB source/match worker: **512 MB**
- DuckDB national/final stage: **1 GB**
- DuckDB threads per connection: **1**
- Overture worker processes: **1**
- Foursquare worker processes: **2** (network-heavy, not CPU-heavy)
- match worker processes: **1**

DuckDB is allowed to spill sorts/joins to `--temp-dir`; put this on an SSD when
possible. The purpose is not to maximize instantaneous CPU utilization. It is
to finish a national run without paging the laptop into unusability.

## Matching strategy

1. Normalize names once during source ingestion.
2. Partition source data into 1° Parquet tiles.
3. Split matching into 0.25° core tiles.
4. Use a ~150 m numeric grid to generate only neighboring candidates.
5. Compute exact Haversine distance and discard >120 m candidates.
6. Use DuckDB-native Jaro-Winkler on normalized name and sorted-name tokens.
7. Apply distance-dependent name thresholds.
8. Greedily cluster the best links while forbidding more than one observation
   from the same source in one canonical POI.

The final point is the median longitude/latitude of the observations in the
accepted cluster. Every original source ID/name/category remains in the output
for auditability.

## Provenance caveat

`source_count` means the POI was observed in that many of the three downloaded
layers. `known_independent_source_count` additionally discounts a visible case
where an Overture record declares Foursquare provenance. It should **not** be
interpreted as a proof of statistical independence among all upstream data
collection systems.
