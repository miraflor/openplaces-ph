# Changelog

All notable public changes to OpenPlaces PH are documented here.

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
