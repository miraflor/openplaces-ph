# Changelog

## 0.5.0 — old-laptop national edition

- Targets the full Philippines and a 2016/2017-era i7-7700HQ-class laptop.
- Keeps DuckDB at one thread per connection with small explicit memory caps.
- Uses one Overture worker, two FSQ remote-I/O workers, and one match worker by default.
- Pins Overture and Foursquare releases so multi-day resumes do not mix vintages.
- Streams Overture bounding boxes with the official Python client instead of loading GeoDataFrames.
- Adds DuckDB HTTP retries/timeouts and per-FSQ-tile fresh-connection retries.
- Makes the Geofabrik OSM download byte-resumable across transient failures and later invocations.
- Keeps 1° source checkpoints and 0.25° × source-pair matching checkpoints.
- Uses DuckDB-native Jaro-Winkler matching after spatial blocking; no national pandas/GeoPandas table.
- Uses transactional union-find snapshots every ranked-edge row group during final clustering.
- Renames the provenance-adjusted count to `known_independent_source_count` to avoid overstating source independence.
