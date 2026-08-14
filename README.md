# OpenPlaces PH

**An open, provenance-aware geospatial registry of places and establishments in the Philippines.**

OpenPlaces PH builds a canonical **point layer** of Philippine places and establishments by reconciling three open geospatial sources:

1. **Foursquare OS Places**
2. **Overture Maps Places**
3. **OpenStreetMap**, using the Geofabrik Philippines extract

The project is intended as reusable digital public infrastructure. Source observations are preserved, cross-source matches are auditable, provenance is explicit, and the nationwide pipeline is designed to survive interruptions on modest hardware.

> OpenPlaces PH is not an official Philippine government registry. It is an open derived data pipeline whose quality depends on the upstream sources and the entity-resolution rules documented here.

---

## What do I get?

The main output is:

```text
data/output/philippines/canonical_pois.parquet
```

This is a **GeoParquet POINT layer**. Each row is one inferred real-world place or establishment.

Core fields include:

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

You also get:

```text
data/output/philippines/observations.parquet
```

which contains the normalized source observations before canonical clustering, and:

```text
data/output/philippines/match_edges.parquet
```

which records the accepted cross-source candidate links, their distance and similarity scores, and whether each link survives the final one-record-per-source clustering constraint.

A compact run summary is written to:

```text
data/output/philippines/summary.json
```

---

## Why three sources?

No single open POI source is complete or perfectly classified. OpenPlaces PH treats each source as noisy evidence rather than as ground truth.

A record may therefore appear as:

```text
Foursquare:  Mercury Drug   -> Drugstore
Overture:    Mercury Drug   -> pharmacy
OSM:         Mercury Drug   -> amenity=pharmacy
```

and be represented in the canonical layer as one establishment while retaining all three source records.

`source_count` is the number of downloaded source layers represented in a canonical POI. `known_independent_source_count` additionally discounts the visible case where an Overture record declares Foursquare provenance. It is **not** a statistical proof that all upstream collection systems are independent.

---

## Hardware profile

The defaults are intentionally conservative. The reference machine is a roughly decade-old Windows laptop with an **Intel Core i7-7700HQ (4 cores / 8 threads) and 16 GB installed RAM**. The pipeline deliberately behaves as though substantially less memory and sustained CPU performance are safely available, leaving headroom for the operating system, antivirus, filesystem cache, thermal throttling, aging storage, and other applications.

Default resource policy:

| Setting | Default |
|---|---:|
| DuckDB memory per source worker | 512 MB |
| DuckDB memory for national/final stages | 1 GB |
| DuckDB threads per connection | 1 |
| Overture workers | 1 |
| Foursquare workers | 2 |
| Matching workers | 1 |
| Source checkpoint size | 1 degree |
| Match checkpoint size | 0.25 degree x source pair |
| Maximum cross-source match distance | 120 m |

The pipeline is **disk-first**. It does not load a Philippines-wide GeoPandas or Pandas table into RAM. DuckDB is allowed to spill joins and sorts to disk.

An SSD is strongly preferred for the temporary directory, but the pipeline can run on slower storage.

---

## Do I need to download the datasets manually?

**No.** The pipeline acquires and caches the required source data itself.

- OpenStreetMap: downloads the Philippines `.osm.pbf` from Geofabrik.
- Overture: streams only Philippine bounding-box tiles from a pinned Overture release.
- Foursquare: queries the gated FSQ OS Places Parquet release remotely and materializes only Philippine tiles.

The only one-time manual step is authorizing access to the free Foursquare OS Places dataset on Hugging Face.

---

## Installation

### 1. Install Miniconda or another Conda-compatible distribution

Then open **Miniconda Prompt** or PowerShell with Conda initialized.

### 2. Create the environment

From the repository root:

```powershell
conda env create -f environment.yml
conda activate openplaces
```

The environment installs Python, DuckDB, PyArrow, Osmium, the Overture client, Hugging Face tooling, and the other required dependencies.

### 3. Install OpenPlaces PH in editable mode

```powershell
pip install -e .
```

This provides the `openplaces` command.

### 4. Authorize Foursquare once

Accept access to the `foursquare/fsq-os-places` dataset on Hugging Face, then run:

```powershell
hf auth login
```

The authentication token is stored by Hugging Face outside this repository. Do **not** place tokens in source files.

### 5. Verify the installation

```powershell
pytest -q
openplaces --help
```

---

## Recommended full-Philippines run

For older hardware, use separate stages. If your fastest drive is `D:`, for example:

### Stage 1 — acquire and normalize the three sources

```powershell
openplaces --scope philippines --only sources --temp-dir D:\openplaces-temp
```

### Stage 2 — build resumable cross-source match shards

```powershell
openplaces --scope philippines --only match --temp-dir D:\openplaces-temp
```

### Stage 3 — rank links, cluster observations, and build the canonical layer

```powershell
openplaces --scope philippines --only finalize --temp-dir D:\openplaces-temp
```

You can also run the entire pipeline with:

```powershell
openplaces --scope philippines --temp-dir D:\openplaces-temp
```

`python run.py ...` is retained as a repository-local alternative to the installed `openplaces` command.

---

## Stop now, continue later

The pipeline is deliberately crash-resumable.

You may press:

```text
Ctrl+C
```

and later run the same command again. Completed checkpoints are reused.

Durable work units are approximately:

| Stage | Durable unit |
|---|---|
| Geofabrik download | resumable `.part` download |
| OSM preparation | local processing stage |
| Overture | 1 degree tile |
| Foursquare | 1 degree tile |
| Pairwise matching | 0.25 degree tile x source pair |
| Greedy final clustering | ranked-edge row group / transactional checkpoint |

To inspect progress without doing work:

```powershell
openplaces --scope philippines --status --temp-dir D:\openplaces-temp
```

---

## Storage

For a nationwide run, keep generous free space for source caches, Parquet intermediates, and DuckDB spill files.

A practical planning target is:

- **30-40 GB free**: comfortable
- **20 GB free**: may work, but gives less safety margin

All generated data are under `data/`, which is excluded by `.gitignore` and should not be committed to GitHub.

---

## How matching works

OpenPlaces PH does not perform a national all-pairs comparison.

For each small match tile it:

1. reads only source files touching that tile plus a small halo;
2. spatially blocks observations using a coarse coordinate grid;
3. calculates exact Haversine distance for nearby candidates;
4. compares normalized names with DuckDB Jaro-Winkler similarity;
5. uses category similarity only as weak supporting evidence;
6. applies stricter name thresholds as distance increases;
7. makes generic names such as `ATM`, `Bank`, or `Clinic` deliberately difficult to merge; and
8. greedily accepts the strongest links while forbidding a canonical cluster from containing two records from the same source.

This last rule prevents common errors such as merging two Foursquare branches in the same shopping mall into one canonical establishment.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the implementation details.

---

## Canonical coordinates

When multiple source observations form one canonical POI, its coordinates are the median longitude and latitude of the matched observations.

OpenPlaces PH intentionally produces **points**, not building footprints. Building assignment can be implemented later as a separate spatial-enrichment layer without changing the canonical POI model.

---

## Evidence tiers

The current output uses:

```text
single
  observed in one source layer

double
  matched across two source layers

triple
  matched across all three source layers
```

These are evidence labels, not calibrated probabilities.

A triple-source POI can still be wrong, and a high-quality single-source POI can still be real.

---

## Source snapshots and reproducibility

Long national runs can span several sessions. To avoid silently mixing source vintages, OpenPlaces PH pins discovered Foursquare and Overture releases in local cache metadata and reuses them on resume.

To deliberately acquire fresh source snapshots:

```powershell
openplaces --scope philippines --refresh-sources --temp-dir D:\openplaces-temp
```

Changing source snapshots invalidates downstream matching/final outputs as appropriate.

---

## Data licensing

The **software in this repository** is licensed under the MIT License.

The **input and output data are not automatically MIT-licensed**.

The normalized observations preserve an `upstream_license` field. The canonical layer exposes source-specific license fields such as `fsq_license`, `overture_license`, and `osm_license`.

Current upstream regimes include:

- Foursquare OS Places: Apache-2.0
- OpenStreetMap: ODbL-1.0
- Overture Places: provider-dependent, including CDLA-Permissive-2.0, Apache-2.0, and CC0-1.0

See [`DATA_LICENSES.md`](DATA_LICENSES.md) before distributing derived datasets.

---

## Repository layout

```text
openplaces-ph/
├── .github/
│   └── workflows/
│       └── tests.yml
├── config/
│   └── areas.yml
├── docs/
│   └── ARCHITECTURE.md
├── src/
│   └── openplaces_ph/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── db.py
│       ├── finalize.py
│       ├── matching.py
│       ├── sources.py
│       ├── tiles.py
│       └── util.py
├── tests/
│   ├── test_matching.py
│   └── test_tiles.py
├── CHANGELOG.md
├── DATA_LICENSES.md
├── LICENSE
├── README.md
├── environment.yml
├── pyproject.toml
└── run.py
```

---

## Development checks

Before committing changes:

```powershell
conda activate openplaces
pip install -e .
pytest -q
python -m compileall src tests run.py
openplaces --help
```

GitHub Actions also runs the unit tests on pushes and pull requests.

---

## Project status

OpenPlaces PH is an early public release. The current priority is to establish a reproducible nationwide canonical places layer with transparent provenance and conservative matching.

Likely future layers include category harmonization, Philippine industry classification, administrative geography, building association, validation samples, and versioned public data releases.
