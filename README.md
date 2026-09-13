# OpenPlaces PH

**An open, provenance-aware registry of places and establishments in the Philippines.**

OpenPlaces PH reconciles open geospatial place observations into an auditable
GeoParquet point layer with explicit `lon`/`lat` coordinate columns. The pipeline
is designed for reproducible research, bounded memory use, resumability, and inspectable entity
resolution.

## Quick start

```bash
conda env create -f environment.yml
conda activate openplaces-ph
python -m pip install -e .
openplaces doctor
```

List configured acquisition areas:

```bash
openplaces areas
openplaces areas --search <text>
```

Build any configured area:

```bash
openplaces build <area>
openplaces build <area1> <area2>
```

For a place not listed in `config/areas.yml`:

```bash
openplaces build --bbox <west> <south> <east> <north> --name <name>
```

A Philippines-wide run must be explicit:

```bash
openplaces build --philippines
```

A bare `openplaces` only prints help.

## Sources

The default source set is Overture Maps Places and OpenStreetMap. That path
requires no Hugging Face account.

Foursquare OS Places is an optional third source:

```bash
python -m pip install -e ".[foursquare]"
hf auth login
openplaces doctor --with-foursquare
openplaces build <area> --with-foursquare
```

The Hugging Face account must have access to `foursquare/fsq-os-places`.

Advanced source selection:

```bash
openplaces build <area> --sources overture
openplaces build <area> --sources osm
openplaces build <area> --sources foursquare overture osm
```

The selected source set **and effective source-tile inventory** are part of the
durable checkpoint identity, so matching state from another source combination
or land-mask result is never silently reused.

A **single-source build** is allowed, but there is then no cross-source entity
resolution. With one selected source, there are zero match pairs, so every
source observation remains its own canonical singleton with `source_count = 1`.
Use a single source when you want a normalized source layer or a lightweight
pilot, not when you want triangulated deduplication.

**OSM first-run note:** OSM preparation uses the national Philippines Geofabrik
extract even for a local target, then builds a reusable national tile cache.
The CLI warns before the first uncached OSM run. For the lightest first run, use
`--sources overture`.

Note that the land mask used to clip tiles to Philippine land area is derived
from Overture. A run without Overture has no mask unless a cached boundary from
an earlier run exists, so tile coverage will differ. Each completed run records
which boundary release, if any, was applied.

## Output

```text
data/output/<scope>/canonical_pois.parquet
```

Audit and provenance outputs:

```text
observations.parquet
match_edges.parquet
summary.json
run.json            package version, Git SHA/dirty state, config, land-mask state, licensing
ATTRIBUTION.txt     human-readable source attribution
```

See `docs/DATA_DICTIONARY.md`.

## Status and recovery

```bash
openplaces status <area>
```

Ordinary users can stay with `openplaces build <area>`. The three durable stages
remain available when needed:

```bash
openplaces build <area> --stage sources
openplaces build <area> --stage match
openplaces build <area> --stage finalize
```

Re-running a compatible command is safe; completed checkpoints are reused.

## Validate output

```bash
openplaces validate <area>
openplaces validate <area> --strict --json
```

Checks that must pass: duplicate and null canonical IDs, missing names, missing
or out-of-range coordinates, scope leakage, and `source_count` consistency with
source membership and with the selected source set.

Checks that only warn, unless `--strict`: coordinates at exactly (0, 0), null
evidence tier, identical name at an identical rounded location, a missing or
malformed `run.json` (for legacy outputs), or an output built from a dirty Git
working tree. A disagreement between `run.json` and the recorded source set is
a hard failure.

## Disk usage and cleanup

```bash
openplaces storage
openplaces clean <area>            # preview
openplaces clean <area> --yes
openplaces clean <area> --all --yes
openplaces clean --raw-downloads --yes
openplaces clean --hf-cache --yes
openplaces clean --shared-osm-cache --yes
```

All cleanup commands are preview-only unless `--yes` is supplied. Cleanup only
ever touches `data/` under the working root and the Foursquare cache directory;
anything else is refused. Removing the reusable national OSM tile cache needs
its own flag because it is expensive to rebuild.

## Environment

| Variable | Effect |
|---|---|
| `OPENPLACES_ROOT` | Working root for `data/` and `config/`. Defaults to the checkout. |
| `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE`, `HF_HOME` | Used to locate the Foursquare cache for `storage` and `clean --hf-cache`. |

## Backwards compatibility

The 0.2 flag interface remains available and is preserved in `legacy_cli.py`:

```bash
openplaces --areas <area> --only sources
openplaces legacy --areas <area> --status
```

Delegation to that interface prints a notice on stderr. `openplaces legacy` with
no arguments is refused, because in 0.2 a bare call started a national build.

The legacy interface retains the 0.2 **all-three-source** behavior. Therefore a
legacy acquisition still requires the optional Foursquare install and approved
Hugging Face access (`python -m pip install -e ".[foursquare]"`). The new
`openplaces build ...` interface is the recommended zero-auth default.

## Matching model

The tested matching model is unchanged in this refactor.

Candidate generation uses spatial blocking, exact Haversine distance, normalized
name similarity, and weak category support. The default acceptance ladder is:

| Distance | Minimum name score |
|---:|---:|
| 0-20 m | 0.70 |
| 20-50 m | 0.82 |
| 50-90 m | 0.90 |
| 90-120 m | 0.94 |

Short and generic names receive stricter treatment.

Accepted links are processed strongest-first by constrained union-find. A
canonical entity may contain at most one observation from each selected source.
The ranking has stable secondary keys down to upstream source IDs, so equal
scores do not leave clustering order to chance for the same inputs and
configuration.

## Installation notes

The reference Conda environment includes the external `osmium` executable used
for OpenStreetMap source preparation. A plain editable pip install cannot install
Osmium; install it separately if you use another environment.

## Data licensing

The software is MIT licensed. Upstream data retain their own licences and
attribution requirements. OpenPlaces records those source terms and the
per-record licence values derived by the pipeline from preserved provenance. It
does **not** assign one blanket licence to the combined output.

| Source | Upstream licence treatment |
|---|---|
| OpenStreetMap | ODbL 1.0 |
| Overture Maps, Places theme | Multi-license by upstream provider; use preserved provenance and the pipeline-derived per-record values |
| Foursquare OS Places | Apache 2.0 |

Every completed build through the 0.3 task-oriented interface writes
`ATTRIBUTION.txt`; `openplaces attribution <area>` regenerates it. The file is deliberately cautious: OSM attribution and possible
ODbL obligations are surfaced, but the software does not make a legal
conclusion that the entire combined database must use a particular licence.
See `DATA_LICENSES.md` and verify current upstream terms before publication.

## Project status

0.3.3 is a usability, source-selection, disk-hygiene, provenance, and validation
refactor over the tested 0.2.1 core. It deliberately leaves matching calibration
and clustering logic unchanged.

The next data-model change should preserve richer source-native semantic fields
for downstream PSIC/PCPC/PSCC classification. That change is intentionally
separate from this refactor.
