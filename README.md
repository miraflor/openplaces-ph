# Philippine POI Triangulation
## Old-laptop / whole-Philippines edition

Build one **canonical Philippine point layer of establishments / POIs** by
triangulating three open sources:

1. **Foursquare OS Places**
2. **Overture Maps Places**
3. **OpenStreetMap**, using the Geofabrik Philippines `.osm.pbf`

The production target for this edition is deliberately modest hardware:

- Intel **Core i7-7700HQ** (4 physical cores / 8 threads)
- **16 GB installed RAM**. RAM capacity does not normally "wear down" with age;
  nevertheless, the pipeline deliberately budgets only a small fraction of it
  so Windows, filesystem cache, antivirus, and other applications retain ample
  headroom
- old laptop storage, cooling, and possible thermal throttling
- Windows 10/11 + Conda

The code favors **survivability and reproducibility over maximum benchmark
speed**. You can press `Ctrl+C`, shut the computer down, and resume later with
minimal lost work.

---

# What do I get?

The main output is:

```text
data/output/philippines/canonical_pois.parquet
```

It is a **GeoParquet point layer**. Each row is one inferred real-world POI,
with the source records retained side by side:

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

overture_id
overture_name
overture_category
overture_provenance

osm_id
osm_name
osm_category
```

You also get:

```text
data/output/philippines/observations.parquet
```

Every normalized original source record, and:

```text
data/output/philippines/match_edges.parquet
```

An audit trail showing which cross-source records were linked, their distance,
name/category similarities, total score, and whether the link survived final
one-record-per-source clustering.

The output is **not yet mapped to AFSA / EDUC / MFG / TRD / etc.** That should be
a separate downstream classification step after the canonical POIs are stable.

---

# Do I manually download the three datasets?

**No.**

The pipeline does the data acquisition itself.

There is only one one-time manual access step: Foursquare OS Places is available
through a gated Hugging Face dataset, so you must accept access and authenticate.

Everything else is automatically downloaded, subsetted, normalized, cached,
and resumed.

---

# One-time setup

## 1. Create the Conda environment

From the repository directory:

```powershell
conda env create -f environment.yml
conda activate ph-poi
```

This installs Python plus `osmium-tool`. The official Overture Python client is
installed inside the environment and is used in streaming mode; you do not run
its CLI manually. The environment pins `overturemaps==1.0.1` so the streaming
API used by this repository does not change halfway through a reproducible run.

## 2. Authorize Foursquare once

Sign in to Hugging Face, open:

```text
foursquare/fsq-os-places
```

accept the dataset conditions, then run:

```powershell
hf auth login
```

Use a read token.

The token is stored by Hugging Face on your computer. DuckDB later obtains it
through its Hugging Face `credential_chain`; the token is never committed to
this repository.

---

# Recommended production run

For this laptop, I recommend running the three large phases separately.

Assuming `D:\ph-poi-temp` is on the drive with the most free space:

## Phase 1 — acquire and normalize all three sources

```powershell
python run.py --scope philippines --only sources --temp-dir D:\ph-poi-temp
```

When that finishes, you can shut the laptop down.

## Phase 2 — triangulate

```powershell
python run.py --scope philippines --only match --temp-dir D:\ph-poi-temp
```

Again, you can stop and resume.

## Phase 3 — create the canonical point layer

```powershell
python run.py --scope philippines --only finalize --temp-dir D:\ph-poi-temp
```

Or simply use one command throughout:

```powershell
python run.py --scope philippines --temp-dir D:\ph-poi-temp
```

Running that same command again is safe: completed checkpoints are detected and
skipped.

---

# Old-laptop defaults

The defaults are intentionally conservative:

```text
remote source block             1.00 degree
matching checkpoint             0.25 degree
Overture workers                    1
Foursquare workers                  2
matching workers                    1
DuckDB memory / worker           512 MB
DuckDB memory / national stage     1 GB
DuckDB threads / connection          1
maximum cross-source distance      120 m
```

Why not use all 8 logical CPU threads?

Because on an old laptop the national job is usually constrained by a
combination of RAM, disk I/O, remote I/O, and heat. Eight competing analytical
threads can make the computer page to disk and thermal-throttle, producing a
*slower* end-to-end run.

Foursquare gets two single-threaded workers because overlapping network latency
can help. Overture defaults to one worker because its official streaming reader
already performs internal I/O readahead. The CPU/disk-heavy matching phase also
defaults to one worker.

If the machine remains cool and Task Manager shows plenty of free RAM, you can
experiment later with:

```powershell
python run.py --scope philippines --match-workers 2 --temp-dir D:\ph-poi-temp
```

Do not start there.

If the laptop struggles, use:

```powershell
python run.py --scope philippines `
  --overture-workers 1 `
  --fsq-workers 1 `
  --match-workers 1 `
  --worker-memory 384MB `
  --main-memory 768MB `
  --temp-dir D:\ph-poi-temp
```

---

# How interruption/resume works

The pipeline never treats "the Philippines" as one indivisible computation.

## OSM

```text
resumable 600-ish MB national PBF download
    -> filtered POI PBF checkpoint
    -> GeoJSONSeq checkpoint
    -> normalized 1-degree Parquet cache
```

The big HTTP download resumes from its `.part` file when the server supports
byte ranges.

## Overture

The pipeline first pins the current Overture release from the official STAC
catalog. Each land-intersecting **1-degree block** is then streamed from that
exact release into an independent atomic Parquet checkpoint. If one block
fails, previously completed blocks are not touched. Resuming days later still
uses the same pinned release, so a monthly Overture update cannot silently mix
source vintages within one run.

## Foursquare

The global FSQ dataset is **not downloaded**. DuckDB queries the gated remote
Parquet release with:

```text
country = PH
+ one 1-degree bbox
```

Each completed Philippine block becomes a permanent local checkpoint. Remote
HTTP reads use DuckDB's built-in retry controls, and a failed block is retried
with a fresh bounded-memory DuckDB connection before the program gives up.

The selected FSQ release date is cached. Overture is pinned in the same way.
Resuming a run therefore does not mix records from different releases. Use
`--refresh-sources` only when you actually want a new source snapshot.

## Matching

Each **0.25-degree tile × source pair** is one atomic checkpoint:

```text
FSQ       <-> Overture
FSQ       <-> OSM
Overture  <-> OSM
```

Completed edge files are never recomputed unless matching settings change or
`--rebuild-match` is supplied.

## Final clustering

Cross-source links are globally ranked, then processed by a compact union-find.
Its three numeric arrays use roughly ten bytes per source observation. The
state is transactionally snapshotted after each 100,000-edge Parquet row group.
A hard stop therefore replays only the unfinished row group rather than all
national clustering.

---

# Why this version is faster than the earlier low-RAM implementation

The most important optimization is not more parallelism. It is **doing less
repeated work**.

### Names are normalized once

At source ingestion, each POI gets:

```text
name_norm
name_tokens
```

`name_tokens` is the same normalized name with tokens alphabetically sorted.

### Matching stays inside DuckDB

The previous design sent candidate rows to Python/RapidFuzz. This edition uses
DuckDB's native string functions:

```text
jaro_winkler_similarity(name_norm)
jaro_winkler_similarity(name_tokens)
```

and takes the better score. This handles both minor spelling variation and many
word-order differences while avoiding millions of Python-level function calls.

### Spatial blocking happens before fuzzy matching

A ~150 m numeric grid generates only neighboring-cell candidates. Exact
Haversine distance then removes anything beyond 120 m before string scoring.
There is never a Philippines-wide Cartesian product.

### FSQ and Overture blocks are sorted by coordinate

Their local 1-degree Parquet files are written `ORDER BY lon, lat` with small
row groups. Subsequent 0.25-degree bbox filters can therefore benefit from
Parquet row-group min/max pruning rather than repeatedly reading every row of
the larger block.

### Pandas is not part of the national pipeline

The job uses DuckDB + Arrow/NumPy. Large country-wide dataframes are never
created in Python.

---

# Matching policy

Candidate maximum distance:

```text
120 metres
```

Default acceptance thresholds:

| Distance | Minimum name similarity |
|---:|---:|
| <= 20 m | 0.70 |
| <= 50 m | 0.82 |
| <= 90 m | 0.90 |
| <= 120 m | 0.94 |

Generic or very short names (`ATM`, `bank`, `shop`, etc.) require approximately
an exact name within 15 m.

Accepted links are ranked approximately as:

```text
80% name similarity
18% spatial closeness
 2% category-string similarity
```

Category receives little weight because FSQ, Overture, and OSM do not use the
same taxonomy.

The final clustering enforces:

> one FSQ + one Overture + one OSM observation maximum per canonical POI

This is specifically intended to reduce false merging of multiple branches in
malls and other dense complexes.

Overture provenance is retained. If an Overture record itself came from
Foursquare, `source_count` may be 2 while `known_independent_source_count` is only 1.

`known_independent_source_count` is deliberately named conservatively: it only
corrects dependencies that are visible in the downloaded provenance. It is **not**
a statistical guarantee that all upstream collection pipelines are independent.

---

# Check progress

At any time:

```powershell
python run.py --scope philippines --status --temp-dir D:\ph-poi-temp
```

Typical output:

```text
OSM normalized cache: ready
Overture 1° blocks: 63/84
Foursquare 1° blocks: 41/84
Match checkpoints: 0/4032
canonical_pois.parquet: not ready
```

The exact block count depends on the current Philippine land mask.

---

# How long will the whole Philippines take?

Do not interpret these as benchmarks. Remote throughput, disk type, antivirus,
Windows background activity, and laptop temperature can change the result by
several times.

For an aging i7-7700HQ laptop, with conservative one-thread analytical work
and only two concurrent workers during the network-heavy Foursquare stage, a
reasonable *planning* range is:

```text
OSM acquisition/preparation       ~20–90 min
Overture Philippine blocks        ~45 min–3.5 h
Foursquare Philippine blocks      ~1.5–5 h
matching                          ~1.5–5 h
finalization                      ~30 min–2 h
------------------------------------------------
rough total                        ~6–15 h first run
```

An HDD, slow connection, antivirus scanning, severe thermal throttling, or a
remote-service slowdown can push the run beyond that. **Budget an overnight run
rather than depending on the lower end of the estimate.**

Because almost everything is checkpointed, elapsed wall-clock time matters much
less: it can be accumulated over several sessions.

---

# Disk space

The current Geofabrik Philippines PBF is only around 600 MB, but temporary
GeoJSON, source Parquet caches, edge shards, external sorts, and DuckDB spill
files dominate the working footprint.

Recommended:

```text
20 GB free       bare minimum target
30–40 GB free    much more comfortable
```

Use an SSD for `--temp-dir` if one exists. If the laptop only has an HDD, the
pipeline still works, but matching/final sorting can be substantially slower.

---

# Refreshing the data later

Normal reruns use the cached source snapshot:

```powershell
python run.py --scope philippines --temp-dir D:\ph-poi-temp
```

To deliberately acquire current versions of all three sources and invalidate
old matching:

```powershell
python run.py --scope philippines --refresh-sources --temp-dir D:\ph-poi-temp
```

Do this only when you actually want a new data vintage.

---

# Repository layout

```text
ph-poi-triangulation/
├── .github/workflows/tests.yml
├── .gitignore
├── README.md
├── CHANGELOG.md
├── LICENSE
├── environment.yml
├── pyproject.toml
├── run.py
├── config/
│   └── areas.yml
├── docs/
│   └── ARCHITECTURE.md
├── src/ph_poi/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── db.py
│   ├── finalize.py
│   ├── matching.py
│   ├── sources.py
│   ├── tiles.py
│   └── util.py
└── tests/
    ├── test_matching.py
    └── test_tiles.py
```

`data/` is deliberately in `.gitignore`. **GitHub stores the reproducible
pipeline, not gigabytes of source data or generated checkpoints.**

---

# External documentation used by the implementation

- Overture Python client: https://docs.overturemaps.org/getting-data/overturemaps-py/
- Overture STAC catalog: https://docs.overturemaps.org/getting-data/cloud-sources/
- DuckDB configuration: https://duckdb.org/docs/current/configuration/overview
- DuckDB text similarity: https://duckdb.org/docs/stable/sql/functions/text
- Hugging Face gated dataset + DuckDB auth: https://huggingface.co/docs/hub/datasets-duckdb-auth
- Geofabrik Philippines OSM: https://download.geofabrik.de/asia/philippines.html

---

# Data licensing / provenance

The **code** in this repository is MIT-licensed. The downloaded data are not
relicensed by this repository. Keep the source IDs and provenance columns in
published derivatives and comply with the licenses/attribution requirements of
Foursquare OS Places, the relevant Overture source records, and OpenStreetMap.

In particular, do not interpret the MIT `LICENSE` file as applying to the
contents of `data/`. `data/` is intentionally gitignored for this reason as well
as for size.
