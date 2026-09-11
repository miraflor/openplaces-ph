# OpenPlaces PH data dictionary

This document describes the public output fields. Types may be represented slightly differently by different Parquet/GeoParquet readers, but the meanings below are the project contract.

## `canonical_pois.parquet`

| Field | Meaning |
|---|---|
| `canonical_id` | Stable-looking identifier chosen from the preferred available source ID (`fsq:...`, otherwise `overture:...`, otherwise `osm:...`). It is stable only as long as the relevant upstream observation remains in the cluster. |
| `canonical_name` | Preferred display name, using explicit source priority FSQ → Overture → OSM. |
| `canonical_category` | Preferred source category using the same priority. If the preferred source has no category, the next source's category is used, so this column can mix the FSQ, Overture, and OSM vocabularies, and it can come from a different source than `canonical_name`. |
| `lon` | Median longitude of cluster members. |
| `lat` | Median latitude of cluster members. |
| `geometry` | GeoParquet point in `OGC:CRS84` (X=longitude, Y=latitude). |
| `source_count` | Number of source layers represented: 1, 2, or 3. |
| `known_independent_source_count` | `source_count` minus one when FSQ and Overture are both present and Overture explicitly declares Foursquare provenance. This is conservative metadata, not proof of statistical independence. |
| `evidence_tier` | `single`, `double`, or `triple`, based only on `source_count`. |
| `match_score_min` | Weakest ranking score among threshold-accepted links that connect members inside the final cluster. Null for singletons. |
| `cluster_max_pair_distance_m` | Largest Haversine separation between any two observations in the cluster. Null for singletons. |
| `completed_transitively` | `true` for a three-source cluster held together by fewer than all three possible accepted pairwise links. |
| `overture_has_foursquare_provenance` | Explicit flag that the Overture member visibly declares Foursquare provenance. |
| `fsq_id` | Foursquare source ID, if present. |
| `fsq_name` | Original Foursquare name. |
| `fsq_category` | Original normalized-source Foursquare category representation. |
| `fsq_license` | Upstream license identifier recorded for the Foursquare observation. |
| `overture_id` | Overture GERS/place ID, if present. |
| `overture_name` | Original Overture primary name. |
| `overture_category` | Overture category representation selected by the source normalizer. |
| `overture_provenance` | Preserved Overture `sources` provenance serialized as text. |
| `overture_license` | License inferred conservatively from the provider dataset names in the preserved Overture provenance (re-derived at every finalization). Unknown/mixed cases are marked rather than guessed. |
| `osm_id` | OSM object identifier such as `node/...`, `way/...`, or `relation/...`, if present. |
| `osm_name` | Original OSM name. |
| `osm_category` | Compact category representation assembled from POI-relevant OSM tags. |
| `osm_license` | `ODbL-1.0`. |

## `observations.parquet`

This is the normalized pre-clustering layer. It is useful when users want to reproduce, audit, or replace the canonicalization logic.

| Field | Meaning |
|---|---|
| `row_id` | Dense integer ID used internally by finalization. It may change after a rebuild and should not be treated as a permanent external identifier. |
| `source` | `fsq`, `overture`, or `osm`. |
| `source_id` | Original source-specific identifier. |
| `name` | Original/preferred source name retained by normalization. |
| `category` | Source category/tag representation. |
| `lon`, `lat` | Normalized point coordinates. |
| `provenance` | Provider provenance where available; chiefly used for Overture. |
| `upstream_license` | License metadata for the source observation. |
| `source_code` | Internal bit code: FSQ=1, Overture=2, OSM=4. |
| `source_priority` | Internal canonical-field priority: FSQ=0, Overture=1, OSM=2. |

`name_norm` and `name_tokens` exist in the **source tile caches** used for matching, not in the final compact observation layer.

## `match_edges.parquet`

Every row is a threshold-accepted cross-source link.

| Field | Meaning |
|---|---|
| `left_id`, `right_id` | Dense observation IDs. |
| `left_source`, `right_source` | Source names. |
| `left_source_id`, `right_source_id` | Upstream source IDs. |
| `distance_m` | Haversine distance between the candidate observations. |
| `name_score` | Maximum Jaro-Winkler score across normalized name and sorted-token name. |
| `category_score` | Weak supporting category-name similarity. |
| `score` | Ranking score used to order accepted edges before greedy clustering. |
| `independent` | `false` for the visible FSQ↔Overture case where Overture provenance declares Foursquare; otherwise `true`. This is not a universal statistical-independence claim. |
| `same_cluster_final` | Whether both endpoints remain in the same final cluster after the one-observation-per-source constraint is applied. |

## `summary.json`

| Key | Meaning |
|---|---|
| `observations` | Number of normalized source observations included in finalization. |
| `candidate_links_accepted_by_thresholds` | Number of cross-source edges written by matching and resolved into finalization. |
| `accepted_links_within_final_cluster` | Number of accepted edges whose endpoints are in the same final canonical entity. |
| `canonical_pois` | Number of canonical entities. |
| `canonical_pois_completed_transitively` | Number of canonical entities with `completed_transitively=true`. |
| `evidence_tiers` | Counts of canonical rows by `single` / `double` / `triple`. |
| `sources` | The source snapshot behind these outputs: `fsq_release`, `overture_release`, and `osm` (the OSM tile cache record, including the Geofabrik PBF version). |
