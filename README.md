# OpenPlaces PH

**An open, provenance-aware registry of places and establishments in the Philippines.**

OpenPlaces PH combines three open geospatial sources into a single auditable point layer:

* **Foursquare OS Places**
* **Overture Maps Places**
* **OpenStreetMap**, from the Geofabrik Philippines extract

Rather than treating any one source as authoritative, the pipeline links observations that appear to describe the same real-world place, preserves the original source records, and records the evidence behind each resulting canonical entity.

The project is designed for reproducible research and public-data work, with an emphasis on **provenance, resumability, bounded memory use, and inspectable entity resolution**.

> OpenPlaces PH is not an official Philippine government registry. It is a derived open-data product whose coverage and accuracy depend on its upstream sources and on the matching rules documented in this repository.

---

## What it produces

The main output is a GeoParquet point layer:

```text
data/output/<scope>/canonical_pois.parquet
```

Each row represents one inferred place or establishment.

Typical fields include:

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
overture_id
osm_id
```

Source-specific names, categories, provenance, and licensing fields are retained as well.

Two audit tables accompany the canonical layer:

```text
observations.parquet
match_edges.parquet
```

`observations.parquet` contains the normalized records before entity resolution.

`match_edges.parquet` contains the accepted cross-source candidate links and their matching evidence.

A machine-readable run summary is also written:

```text
summary.json
```

See [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) for field definitions.

---

## Why this exists

Open geographic datasets overlap, but they do not describe places in exactly the same way.

A restaurant might appear:

* under slightly different names;
* a few metres apart;
* with different categories;
* in two or three source datasets;
* or indirectly in one dataset through another provider.

Simply concatenating the sources therefore produces duplicates. Blind deduplication, on the other hand, can collapse genuinely distinct establishments.

OpenPlaces PH treats this as an **entity-resolution problem**.

The pipeline keeps the observations separate, generates plausible cross-source links, scores those links using location and names, and then constructs canonical entities subject to explicit constraints.

The result is not intended to hide uncertainty. It is intended to make the reconciliation process inspectable.

---

## Pipeline

At a high level:

```text
Foursquare ─────┐
                │
Overture ───────┼──> normalize ──> candidate links ──> score ──> cluster ──> canonical POIs
                │
OpenStreetMap ──┘                         │                │
                                         │                │
                                  match_edges.parquet     │
                                                          │
                                                observations.parquet
```

The pipeline has three durable stages:

```text
sources  ->  match  ->  finalize
```

Each stage can be run separately and resumed later.

---

## Installation

### 1. Clone the repository

```powershell
git clone https://github.com/miraflor/openplaces-ph.git
cd openplaces-ph
```

### 2. Install the environment

The reference environment is defined in `environment.yml`.

```powershell
conda env create -f environment.yml
conda activate openplaces
```

If you already maintain your own Conda environment, install the required dependencies there instead.

### 3. Install OpenPlaces PH

For the current release, use an editable install from the repository:

```powershell
python -m pip install -e .
```

This creates the `openplaces` command.

### 4. Authenticate with Hugging Face

Foursquare OS Places is accessed through Hugging Face.

After accepting access to the dataset:

```powershell
hf auth login
```

Do not place access tokens in the repository.

### 5. Verify the installation

```powershell
openplaces --help
python -m pytest -q
```

---

## Start with a small area

Before attempting a national build, it is sensible to exercise the complete pipeline on a smaller scope.

For example:

```powershell
openplaces --areas metro_manila --only sources --temp-dir D:\openplaces-temp
openplaces --areas metro_manila --only match --temp-dir D:\openplaces-temp
openplaces --areas metro_manila --only finalize --temp-dir D:\openplaces-temp
```

Outputs will be written under the corresponding area directory in `data/output/`.

You can inspect the resulting GeoParquet directly in QGIS, DuckDB, Python, or another GeoParquet-aware tool.

---

## Philippines-wide run

A national run can be executed stage by stage:

### Acquire and normalize source data

```powershell
openplaces --scope philippines --only sources --temp-dir D:\openplaces-temp
```

### Generate cross-source matches

```powershell
openplaces --scope philippines --only match --temp-dir D:\openplaces-temp
```

### Build the canonical registry

```powershell
openplaces --scope philippines --only finalize --temp-dir D:\openplaces-temp
```

Or run the complete pipeline:

```powershell
openplaces --scope philippines --temp-dir D:\openplaces-temp
```

Check current state without starting new work:

```powershell
openplaces --scope philippines --status --temp-dir D:\openplaces-temp
```

---

## Resumability

OpenPlaces PH is built around durable checkpoints.

A long run may be interrupted and continued later. Completed units are reused rather than recomputed.

Examples include:

| Stage              | Durable unit                   |
| ------------------ | ------------------------------ |
| Geofabrik download | resumable partial download     |
| OSM preparation    | prepared local partitions      |
| Foursquare         | 1° source tile                 |
| Overture           | 1° source tile                 |
| Pairwise matching  | 0.25° tile × source pair       |
| Final clustering   | transactional clustering state |

Version 0.2.1 also binds downstream work to the source snapshot from which it was generated.

This means, for example, that match shards from an older source release are not silently reused after a source refresh.

Completed matching records the expected shard inventory, and finalization verifies that the expected shards are still present and valid before using them.

The principle is simple:

> **Partial, stale, or internally inconsistent state should stop the pipeline rather than quietly become published output.**

---

## Entity matching

Matching is performed separately for each pair of sources:

```text
Foursquare <-> Overture
Foursquare <-> OpenStreetMap
Overture   <-> OpenStreetMap
```

OpenPlaces PH does not perform a national all-pairs comparison.

Candidate generation uses spatial blocking so that only geographically plausible observations are compared.

For each match tile, the pipeline roughly performs:

1. spatially restrict the source observations;
2. assign observations to small coordinate-grid cells;
3. compare observations only across neighboring cells;
4. eliminate impossible pairs using cheap coordinate bounds;
5. compute exact Haversine distance for surviving candidates;
6. compare normalized names using Jaro-Winkler similarity;
7. use category agreement as supporting evidence; and
8. retain links that satisfy a distance-dependent acceptance threshold.

The default acceptance ladder is:

| Distance | Minimum name score |
| -------: | -----------------: |
|   0–20 m |               0.70 |
|  20–50 m |               0.82 |
|  50–90 m |               0.90 |
| 90–120 m |               0.94 |

Short or generic labels such as `ATM`, `Bank`, `Shop`, or `Clinic` receive stricter treatment because they are especially prone to false matches in dense environments.

The acceptance rules have both SQL and Python representations and are tested for parity.

---

## Clustering

Accepted links are ranked strongest-first and processed by a constrained union-find algorithm.

The central constraint is:

> **A canonical entity may contain at most one observation from each source.**

This prevents nearby establishments from the same provider from being collapsed into one entity through a third source.

For example, two separate Foursquare branches cannot both become members of the same canonical POI.

Three-source entities do not need to contain all three possible direct links.

A cluster may therefore arise as:

```text
Foursquare <-> Overture <-> OSM
```

even when no direct Foursquare–OSM link was accepted.

These cases remain visible through fields such as:

```text
completed_transitively
cluster_max_pair_distance_m
```

so downstream users can impose stricter criteria if needed.

---

## Provenance

OpenPlaces PH preserves source identity rather than erasing it during canonicalization.

The canonical output retains identifiers and attributes from the contributing datasets, including source-specific licensing and provenance information.

`source_count` records how many source layers contribute to an entity. It should not be interpreted as a probability that the entity is correct.

The pipeline also distinguishes:

```text
known_independent_source_count
```

because an Overture record may itself identify Foursquare as an upstream provider.

This adjustment is deliberately conservative. It captures known provenance relationships without claiming statistical independence that cannot be established from the available metadata.

---

## Designed for constrained hardware

The pipeline was designed around a modest Windows machine rather than a large server.

The reference design assumes approximately:

* 8 GB RAM;
* a four-core laptop-class CPU;
* limited tolerance for large in-memory dataframes; and
* local disk available for intermediate files and DuckDB spill.

Accordingly:

* nationwide tables are not loaded into Pandas or GeoPandas;
* joins, filtering, and sorts are delegated to DuckDB;
* intermediate data are checkpointed as Parquet;
* matching operates on small spatial partitions;
* final clustering uses a compact union-find representation; and
* the pipeline is expected to survive interruption.

The design goal is not maximum throughput. It is to make a national-scale reconciliation pipeline feasible on ordinary hardware.

---

## Refreshing source data

To request newer upstream data:

```powershell
openplaces --scope philippines --refresh-sources --only sources --temp-dir D:\openplaces-temp
```

A refresh does not mean blindly redownloading everything.

The pipeline records source-release identities and associates normalized source directories, match checkpoints, and final outputs with the snapshots from which they were generated.

When the source snapshot changes, downstream work is invalidated and rebuilt as necessary.

---

## Tests

Run the complete suite with:

```powershell
python -m pytest -q
```

The test suite covers areas including:

* spatial-blocking recall;
* SQL versus brute-force candidate generation;
* distance-threshold parity;
* name-normalization parity;
* constrained clustering;
* deterministic resume behavior;
* source-release snapshots;
* interrupted refreshes;
* incomplete and stale match shards;
* HTTP range resume and upstream file changes;
* OSM preparation;
* Overture normalization and provenance;
* empty-output cases;
* GeoParquet metadata; and
* finalization state integrity.

Compile-time syntax checking can also be run with:

```powershell
python -m compileall -q src tests run.py
```

---

## Repository structure

```text
openplaces-ph/
├── config/
│   └── areas.yml
├── docs/
│   ├── ARCHITECTURE.md
│   ├── CODE_WALKTHROUGH.md
│   ├── DATA_DICTIONARY.md
│   └── REVIEW-2026-09.md
├── src/
│   └── openplaces_ph/
├── tests/
├── environment.yml
├── pyproject.toml
└── run.py
```

Useful documentation:

* [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — pipeline architecture and design decisions
* [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) — implementation walkthrough
* [`docs/DATA_DICTIONARY.md`](docs/DATA_DICTIONARY.md) — output schema
* [`docs/REVIEW-2026-09.md`](docs/REVIEW-2026-09.md) — review of the 0.2.1 state and refresh changes
* [`DATA_LICENSES.md`](DATA_LICENSES.md) — source-data licensing and attribution

---

## Known limitations

OpenPlaces PH is still an evolving research/data-engineering project.

Important current limitations include:

**Non-Latin names.**
The current normalization pipeline is ASCII-oriented. Records whose names contain no usable Latin letters or digits may not participate correctly in matching and can be dropped from the canonical layer.

**Category harmonization.**
The three upstream sources use different category systems. `canonical_category` therefore does not yet represent a fully harmonized national taxonomy.

**Canonical-name priority.**
The preferred display name is selected using a fixed source priority. That choice has not yet been systematically evaluated across Philippine regions or establishment types.

**Kalayaan municipality.**
The current default Philippines bounding box begins at 116.80°E and therefore does not include the Kalayaan municipality / Pag-asa Island area near 114.3°E.

**National-scale validation.**
The software has extensive automated tests, including state and resume behavior, but a complete real-source national run remains a separate operational validation step.

These are documented limitations rather than hidden assumptions.

---

## Data licensing

The **software** in this repository is released under the MIT License.

The **source datasets and derived data do not automatically inherit the MIT License**.

OpenPlaces PH retains source and license information in its outputs where available.

Before redistributing derived data, consult:

[`DATA_LICENSES.md`](DATA_LICENSES.md)

and the current terms of the relevant upstream datasets.

---

## Project status

**Current development version: 0.2.1**

Version 0.2.1 focuses on making stop/resume and source refresh behavior safe across separate sessions, strengthening checkpoint integrity, and improving reproducibility of downstream outputs.

The project should currently be treated as an auditable data-building pipeline rather than as a finished authoritative registry.

Contributions, reproducibility checks, bug reports, and validation against Philippine ground truth are welcome.

---

## License

MIT License for the software.

See [`LICENSE`](LICENSE) for the code license and [`DATA_LICENSES.md`](DATA_LICENSES.md) for data-source licensing and attribution.
