# Changelog

All notable public changes to OpenPlaces PH are documented here.

## 0.2.0 — correctness and performance pass

### Fixed

- **`--max-distance` now reaches the spatial blocking grid.** `grid_degrees` was
  hard-coded at `0.0015` deg (~156 m at 21.3 N) while `--max-distance` was
  user-configurable. A 3x3 neighbour join only guarantees coverage out to one
  cell width, so a sufficiently large `--max-distance` could silently fail to
  generate genuinely in-range pairs. The cell is now derived from the
  search radius, and `tests/test_grid_coverage.py` proves total recall by
  exhaustive sub-cell sweep across Philippine latitudes.
- **The Python acceptance mirror and the SQL that produces the data can no
  longer drift.** `accept_pair()` hard-coded a final `distance_m <= 120` guard
  that the SQL `ELSE` branch did not have; at `--max-distance 200` the two
  disagreed and the unit tests still passed. Both are now generated from
  `acceptance_bands()`, and `tests/test_threshold_parity.py` evaluates the real
  SQL in DuckDB against the Python function on randomized inputs.
- **Truncating the radius no longer changes the calibrated threshold.** A 60 m
  run now correctly uses the 0.90 rule for 50-60 m rather than incorrectly
  jumping to the >90 m threshold of 0.94. Generic labels also obey a user-set
  radius below their normal 15 m cap.
- **Overture Places are no longer clipped harder than the other two sources.**
  A strict `ST_Within` against the national land polygon dropped coastal,
  reclaimed-land, port, and small-island establishments from Overture only,
  biasing `source_count` and `evidence_tier` downwards along the coastline.
  Replaced with `ST_DWithin` at a ~220 m tolerance.
- **Truncated downloads can no longer be promoted to a permanent cache entry.**
  `resume_download` now verifies the received byte count against
  `content-length` before the atomic rename, and fsyncs the `.part` file so a
  power loss cannot leave a size the data never reached.
- OSM representative points use `ST_PointOnSurface` instead of `ST_Centroid`,
  which is guaranteed to fall inside concave footprints (U-shaped malls,
  ring-shaped markets), and is computed once per row instead of once per axis.
- CRS handling is explicit (`OGC:CRS84`) in Overture boundary operations and
  in the canonical point geometry. Finalization refuses to promote the main
  output if the Parquet footer lacks GeoParquet `geo` metadata.
- Final edge-id resolution now checks that every accepted source edge resolves
  to exactly one observation on each side; missing or duplicate source IDs fail
  loudly rather than silently changing clustering.

### Changed

- The national ranked-edge sort now carries only `(left_id, right_id)`.
  `ranked_edges.parquet` is replaced by `edge_ids.parquet` (unsorted, wide) plus
  `ranked_pairs.parquet` (sorted, narrow). Tie-breaking still uses upstream
  source ids, so ranking stays deterministic across a `--rebuild-finalize`.
  This sharply reduces the amount of data DuckDB must carry through the
  national sort.
- Matching applies separable degree bounds inside the join, so exact Haversine
  is only evaluated for pairs that could plausibly be in range. Combined with
  the tighter derived grid, this reduces expensive candidate calculations
  without changing the acceptance rule.
- `build_observations` assigns `row_id` with a streaming `file_row_number` pass
  instead of `row_number() OVER ()`, which forced DuckDB's window operator to
  materialize the whole national table inside a 1 GB budget.
- Greedy clustering uses a compact `array('I')` parent vector plus one-byte
  rank/source-mask buffers. The **working** state is ~6 bytes per observation,
  rather than using a Python `list[int]` whose integer objects would consume
  hundreds of megabytes to gigabytes on a national run. Final path compression
  is vectorised, and snapshots use a time budget rather than one fsync per edge
  row group.
- `write_summary` uses the same constrained DuckDB connection policy as every
  other stage instead of an unbounded default connection, in two passes rather
  than five table scans.
- `huggingface_hub` and `overturemaps` are imported lazily, so `--status` and
  the unit tests no longer depend on the network client stack.
- Per-tile source file lookups are cached; `valid_parquet` opens a Parquet
  footer, and matching asked the same question tens of thousands of times.

### Added

- `cluster_max_pair_distance_m` and `completed_transitively` on the canonical
  layer. Nothing enforces triangle closure, so a three-source cluster can be
  completed by transitivity with its extremes up to 2x `max_distance` apart and
  no direct link ever accepted between them. These columns make that visible
  instead of invisible.
- `canonical_pois_completed_transitively` in `summary.json`.
- Integration tests for the finalize chain and for resumable clustering,
  including a test that an interrupted run reproduces an uninterrupted one.

## 0.1.0 — initial public release

- Launch the project publicly as **OpenPlaces PH**.
- Rename the Python distribution to `openplaces-ph`.
- Rename the import package to `openplaces_ph`.
- Add the `openplaces` command-line entry point.
- Establish a whole-Philippines, low-memory, crash-resumable processing profile.
- Acquire Foursquare OS Places, Overture Maps Places, and OpenStreetMap as separate provenance-preserving source layers.
- Pin Foursquare and Overture releases so multi-session runs do not silently mix source vintages.
- Use resumable Geofabrik OSM acquisition and Osmium-based filtering rather than loading a national OSM dataset into GeoPandas.
- Use 1-degree source checkpoints and 0.25-degree x source-pair matching checkpoints.
- Perform out-of-core filtering, matching, ranking, and aggregation with DuckDB and Parquet.
- Use conservative spatial blocking and DuckDB-native Jaro-Winkler name matching.
- Enforce at most one observation from each source within a canonical POI cluster.
- Add transactional union-find checkpoints for resumable final clustering.
- Preserve raw source IDs, names, categories, and Overture provenance in final outputs.
- Use `known_independent_source_count` rather than overstating statistical independence among upstream sources.
- Add explicit source-license metadata to normalized and canonical outputs.
- Add `DATA_LICENSES.md` to distinguish the MIT software license from upstream data licenses.
