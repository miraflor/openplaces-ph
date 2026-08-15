# OpenPlaces PH

**An open, provenance-aware point registry of places and establishments in the Philippines.**

OpenPlaces PH reconciles three open geospatial sources into one auditable canonical point layer:

1. **Foursquare OS Places**
2. **Overture Maps Places**
3. **OpenStreetMap**, using the Geofabrik Philippines extract

The project is designed as reusable digital public infrastructure rather than as a one-off data scrape. It preserves the source observations, records why observations were linked, makes provenance and licensing visible, and is deliberately engineered to survive interruption on an aging **8 GB Windows laptop**.

> OpenPlaces PH is not an official Philippine government registry. It is a derived open-data pipeline, and its quality is limited by upstream coverage and by the documented entity-resolution rules.

---

## What the pipeline produces

The main output is:

```text
data/output/philippines/canonical_pois.parquet
```

It is a **GeoParquet POINT layer in OGC:CRS84**. Each row is one inferred real-world place or establishment.

Important fields include:

```text
canonical_id
canonical_name
canonical_category
lon
lat
geometry

source_count
known_independent_source_count
evidence_tier
match_score_min
cluster_max_pair_distance_m
completed_transitively

fsq_id
fsq_name
fsq_category
fsq_license

overture_id
overture_name
overture_category
overture_provenance
overture_license

osm_id
osm_name
osm_category
osm_license
```

Two audit layers are written beside it:

```text
data/output/philippines/observations.parquet
data/output/philippines/match_edges.parquet
```

`observations.parquet` contains the normalized source records before clustering. `match_edges.parquet` contains every threshold-accepted cross-source link, its distance and similarity scores, and whether the final constrained clustering kept both endpoints in the same entity.

A compact run summary is written to:

```text
data/output/philippines/summary.json
```

See [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) for field-by-field definitions.

---

## Design target: a decade-old 8 GB laptop

The defaults are intentionally conservative. The reference profile is roughly:

- Windows laptop
- Intel Core i7-7700HQ-class CPU, 4 cores / 8 threads
- **8 GB RAM**
- aging storage, with an SSD strongly preferred for temporary spill files

The code assumes that Windows, antivirus, the filesystem cache, Python, your browser/editor, and thermal throttling all need headroom. It therefore does **not** try to use every thread or every free byte of RAM.

| Setting | Default |
|---|---:|
| DuckDB memory per source/match worker | 512 MB |
| DuckDB memory for national/final stages | 1 GB |
| DuckDB threads per connection | 1 |
| Overture workers | 1 |
| Foursquare workers | 2 |
| Matching workers | 1 |
| Source checkpoint size | 1 degree |
| Match checkpoint size | 0.25 degree × source pair |
| Maximum cross-source match distance | 120 m |
| Blocking-cell size | derived from the distance |

The pipeline is **disk-first**. It never constructs a Philippines-wide Pandas or GeoPandas table in RAM. DuckDB is allowed to spill joins and sorts to `--temp-dir`.

The national union-find is also compact in its **working** representation: approximately 6 bytes per observation for a 32-bit parent, one-byte rank, and one-byte source mask. It deliberately does not use `list[int]` for millions of parent pointers.

---

## Installation

### 1. Install Miniconda

Use Miniconda, Miniforge, or another Conda-compatible distribution.

### 2. Create the environment

From the repository root:

```powershell
conda env create -f environment.yml
conda activate openplaces
```

The environment fixes Python at 3.11 and DuckDB at 1.5.5, the version against which the v0.2 SQL and geometry behavior were reviewed.

### 3. Install the package

```powershell
pip install -e .
```

This creates the `openplaces` command.

### 4. Authorize Foursquare once

Accept access to the free `foursquare/fsq-os-places` dataset on Hugging Face, then run:

```powershell
hf auth login
```

The token is stored by Hugging Face outside the repository. Do not paste tokens into code or commit them to Git.

### 5. Verify the installation

```powershell
pytest -q
openplaces --help
```

---

## First run: test Metro Manila before the whole country

Before committing hours of machine time to a nationwide run, exercise the real network and spatial code on one manageable area:

```powershell
openplaces --areas metro_manila --only sources --temp-dir D:\openplaces-temp
openplaces --areas metro_manila --only match --temp-dir D:\openplaces-temp
openplaces --areas metro_manila --only finalize --temp-dir D:\openplaces-temp
```

Then inspect:

```text
data/output/areas_metro_manila/canonical_pois.parquet
data/output/areas_metro_manila/summary.json
```

Load the GeoParquet in QGIS and check a few malls, campuses, coastal areas, and dense commercial strips. This is a smoke test of the actual upstream releases and DuckDB spatial extension on your own machine, not merely of unit-test logic.

---

## Recommended full-Philippines run

Running in three stages makes interruption and troubleshooting straightforward.

### Stage 1 — acquire and normalize the sources

```powershell
openplaces --scope philippines --only sources --temp-dir D:\openplaces-temp
```

### Stage 2 — build resumable cross-source match shards

```powershell
openplaces --scope philippines --only match --temp-dir D:\openplaces-temp
```

### Stage 3 — rank links, cluster observations, and write the canonical layer

```powershell
openplaces --scope philippines --only finalize --temp-dir D:\openplaces-temp
```

You may also run everything in one command:

```powershell
openplaces --scope philippines --temp-dir D:\openplaces-temp
```

`python run.py ...` is retained as a repository-local alternative.

---

## Stop now, continue later

You may press `Ctrl+C` and rerun the same command later. Completed durable units are reused.

| Stage | Durable unit |
|---|---|
| Geofabrik download | byte-resumable `.part` file |
| OSM preparation | local stage / final partitioned cache |
| Overture | 1-degree tile |
| Foursquare | 1-degree tile |
| Pairwise matching | 0.25-degree tile × source pair |
| Final clustering | transactional union-find snapshot |

Check progress without starting work:

```powershell
openplaces --scope philippines --status --temp-dir D:\openplaces-temp
```

Temporary and generated data live under `data/` and are ignored by Git.

---

## How matching works

OpenPlaces PH does **not** compare every Philippine POI with every other POI.

For each pair of sources:

```text
Foursquare <-> Overture
Foursquare <-> OSM
Overture   <-> OSM
```

and for each small match tile, it performs the following sequence:

1. read only source files touching the tile plus a small halo;
2. place observations into a coordinate grid whose cell width is derived from `--max-distance`;
3. join only the 3×3 neighboring grid cells;
4. apply cheap latitude/longitude bounds before any trigonometry;
5. calculate exact Haversine distance only for survivors;
6. compare normalized names and sorted-name tokens with Jaro-Winkler similarity;
7. use category similarity only as weak supporting evidence; and
8. retain pairs that satisfy the distance-dependent name threshold.

The default calibrated ladder is:

| Distance | Minimum name score |
|---:|---:|
| 0–20 m | 0.70 |
| 20–50 m | 0.82 |
| 50–90 m | 0.90 |
| 90–120 m | 0.94 |

Generic or very short labels such as `ATM`, `Bank`, `Shop`, or `Clinic` are much more dangerous in malls, airports, campuses, and markets. They therefore require near-exact naming and a distance of at most 15 m.

### A non-default radius does not change the calibration

`--max-distance` changes where the search stops, not the meaning of the intervals.

For example, at `--max-distance 60`, the effective ladder is:

```text
0–20 m   -> 0.70
20–50 m  -> 0.82
50–60 m  -> 0.90
```

It does **not** suddenly require 0.94 between 50 and 60 m.

Both the SQL and the readable Python mirror are generated from `acceptance_bands()`, and the test suite evaluates them against each other across several radii and all threshold edges.

---

## How clustering works

Threshold-accepted links are sorted strongest-first. The clustering stage then performs greedy union-find subject to one central constraint:

> **One canonical entity may contain at most one observation from each source.**

This prevents two Foursquare branches in the same mall, or two nearby OSM establishments, from being collapsed into one canonical POI through a third source.

Only the two dense observation IDs travel through the national sort. Wide strings, provenance, scores, and other attributes remain in `edge_ids.parquet` and are joined back later.

### Transitive triples are visible, not hidden

The algorithm does not require every three-source cluster to contain all three direct pairwise links.

A cluster can therefore exist because:

```text
Foursquare <-> Overture <-> OSM
```

without an accepted direct `Foursquare <-> OSM` link.

The canonical output exposes this with:

- `completed_transitively`: `true` when a three-source cluster has fewer accepted internal links than the three possible pairs;
- `cluster_max_pair_distance_m`: the largest pairwise distance among the observations in that cluster.

This lets downstream users impose a stricter filter without hiding the underlying model choice.

---

## Source-specific handling

### OpenStreetMap

The Philippines Geofabrik PBF is downloaded with HTTP byte-range resume. Osmium filters named objects carrying POI-relevant keys before geometry is exported. Polygons and lines are converted to one representative point using `ST_PointOnSurface`, which stays on the original geometry even for concave footprints.

### Overture Maps

One Overture release is pinned for a resumable run. A Philippine land geometry from the same release is used to remove ocean-only tiles.

Overture is the only source subject to an explicit land-geometry test, so the final point test uses a small tolerance instead of strict containment. This avoids systematically deleting reclaimed-land, port, pier, or slightly seaward coordinates from Overture while leaving equivalent records in Foursquare or OSM.

Geometry is explicitly treated as **OGC:CRS84** so X means longitude and Y means latitude throughout the pipeline.

### Foursquare OS Places

Foursquare is queried remotely through authenticated Hugging Face Parquet. Both `country='PH'` and tile coordinate bounds are applied so the pipeline never materializes the global dataset locally.

---

## Provenance and independence

`source_count` means the number of downloaded source layers represented in a canonical row. It is **not** a probability of correctness.

Overture may itself contain a Foursquare-derived record. Therefore:

```text
known_independent_source_count
```

subtracts the visible Foursquare/Overture dependency when Overture provenance explicitly says so.

It remains deliberately conservative: it does not claim that every other upstream collection process is statistically independent.

---

## Failure checks that deliberately stop the pipeline

The project prefers a loud failure over silently publishing a subtly corrupted registry.

Examples include:

- an HTTP download whose received length does not match the expected length;
- an accepted match edge that cannot resolve to exactly one observation on both sides;
- a union-find run too large for its explicitly 32-bit compact parent representation;
- a canonical Parquet file whose footer lacks GeoParquet `geo` metadata.

These checks can make a run stop, but they are there so a resumable public-data pipeline does not quietly turn partial or ambiguous state into a durable result.

---

## Storage planning

Actual source sizes vary by upstream release, but a nationwide run needs room for cached inputs, normalized source tiles, match shards, final intermediates, and DuckDB spill files.

A practical planning target is:

- **30–40 GB free:** comfortable starting point;
- **20 GB free:** possible but with less safety margin;
- **under 15 GB:** the CLI warns you before work begins.

Put `--temp-dir` on the fastest disk with the most free space.

---

## Reproducibility

A multi-session run must not silently mix source vintages.

- Overture release metadata is pinned in the local cache.
- Foursquare release metadata is pinned in the local cache.
- The downloaded OSM PBF is reused until `--refresh-sources` is requested.
- Matching configuration is fingerprinted beside its durable edge shards.
- Finalization records its dependency configuration and invalidates downstream checkpoints when that contract changes.

To deliberately acquire fresh source snapshots:

```powershell
openplaces --scope philippines --refresh-sources --temp-dir D:\openplaces-temp
```

---

## Tests

Run:

```powershell
pytest -q
python -m compileall -q src tests run.py
openplaces --help
```

The test suite covers, among other things:

- spatial-blocking recall across Philippine latitudes;
- SQL/Python threshold parity at several radii;
- the correct piecewise calibration when a radius is truncated;
- the one-observation-per-source clustering constraint;
- deterministic resume behavior;
- compact `uint32` checkpoint parents;
- dense observation IDs;
- narrow strongest-first ranked pairs;
- canonical transitivity diagnostics;
- Foursquare-in-Overture provenance discounting;
- GeoParquet metadata; and
- lazy loading of the heavy acquisition clients.

CI runs the same tests on Python 3.11.

---

## Data licensing

The **software** in this repository is MIT-licensed.

The **input and derived data are not automatically MIT-licensed**.

Normalized observations preserve `upstream_license`, and canonical rows expose source-specific license fields. Current upstream regimes include Foursquare OS Places, OpenStreetMap ODbL, and provider-dependent Overture Places licensing.

Read [`DATA_LICENSES.md`](DATA_LICENSES.md) before redistributing generated datasets.

---

## Repository layout

```text
openplaces-ph/
├── .github/workflows/tests.yml
├── config/
│   └── areas.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── CODE_WALKTHROUGH.md
│   ├── DATA_DICTIONARY.md
│   └── REVIEW-2026-08.md
├── src/openplaces_ph/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── finalize.py
│   ├── matching.py
│   ├── sources.py
│   ├── tiles.py
│   └── util.py
├── tests/
├── CHANGELOG.md
├── DATA_LICENSES.md
├── LICENSE
├── README.md
├── environment.yml
├── pyproject.toml
└── run.py
```

For a non-programmer-oriented tour of what each module is doing and why, start with [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md).
