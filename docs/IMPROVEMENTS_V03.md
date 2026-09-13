# OpenPlaces PH 0.3 refactor

The goal is to fix the usability problems revealed by the real-source pilot
without replacing the matching and clustering code that already worked.

## What changes

### 1. One normal build command

```powershell
openplaces build <area>
```

The durable stages remain available through `--stage` for recovery and
debugging. The 0.2 flag interface is preserved as `legacy_cli.py`.

### 2. A national run is never the accidental default

A bare `openplaces` prints help. A national run must be explicit:

```powershell
openplaces build --philippines
```

`openplaces legacy` with no arguments is refused, because in 0.2 a bare
invocation started a national build. Delegation to the 0.2 interface now prints
a notice on stderr, so it is never silent.

### 3. Foursquare is optional, not removed

The default source set is Overture + OpenStreetMap, and needs no Hugging Face
account. `huggingface_hub` moves to the `foursquare` extra.

```powershell
python -m pip install -e ".[foursquare]"
openplaces build <area> --with-foursquare
```

The exact source set and effective source-tile inventory are part of the durable
checkpoint identity, so runs with different sources or land-mask results cannot
silently reuse one another's match state.

Single-source mode is explicit: it creates zero cross-source pairs, so each
observation remains a singleton canonical entity. That is useful for normalized
source exports, but it is not triangulated entity resolution.

### 4. Any configured area remains equally valid

`config/areas.yml` is untouched. The CLI accepts several configured areas as one
scope, or an explicit bounding box. Unknown area keys produce a message with
near matches rather than a traceback.

### 5. Disk use becomes visible

```powershell
openplaces storage
```

The Hugging Face cache is located through `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE`
and `HF_HOME` rather than a fixed path, so it is reported correctly for users
who have moved it.

### 6. Cleanup becomes a supported operation

Cleanup is preview-only without `--yes`. Every target is checked against the set
of trees cleanup is allowed to touch, which is `data/` under the working root
plus the Foursquare cache directory. Anything else is refused. Scope names that
contain a path separator are refused. Symlinks are skipped, not followed.

The per-target flag is `--source-tiles`, not `--sources`. In the 0.3 candidate
`--sources` meant a source list on `build` and a deletion switch on `clean`,
which is the kind of collision that produces an accident once.

### 7. Built-in structural validation

```powershell
openplaces validate <area>
openplaces validate <area> --strict --json
```

Checks that must pass: canonical-ID uniqueness, no null IDs, missing names,
missing coordinates, coordinates out of range, points outside the requested
scope, `source_count` consistent with source membership, and `source_count`
within the selected source set.

Checks that only warn, unless `--strict`: coordinates at exactly (0, 0),
null evidence tier, identical name at an identical rounded location, a missing
legacy `run.json`, and outputs built from a dirty Git tree. The validator also
checks that `run.json` agrees with the source set used by the output.

It uses DuckDB directly and does not add pandas. The output path is quoted
safely, so a path containing an apostrophe no longer produces a SQL error.

### 8. Every run records what produced it

A completed build writes `run.json` beside the output:

```json
{
  "openplaces_version": "0.3.3",
  "git_commit": "<sha>",
  "git_dirty": false,
  "active_sources": ["overture", "osm"],
  "pairs": ["overture__osm"],
  "source_tile_count": 6,
  "land_mask_boundary_release": "2026-08-20.0",
  "licensing": { "...": "..." }
}
```

`land_mask_boundary_release` matters because the land mask is derived from
Overture. A run without Overture has no mask unless a cached boundary happens to
exist, so the same command can cover a different number of tiles depending on
cache state. Recording it makes that visible instead of silent.

### 9. Attribution travels with the data

A completed build also writes `ATTRIBUTION.txt`, and `licensing.py` holds
per-source licence metadata in one place. `openplaces attribution <area>`
regenerates it for an existing output.

The attribution layer records upstream terms without deciding the legal licence
of the combined output. Overture is explicitly treated as multi-license and the
licence labels derived from preserved provider provenance and written to
`canonical_pois.parquet` are surfaced when available. OSM attribution and
possible ODbL obligations are flagged, but the software defers the final
publication/licensing conclusion to the concrete release architecture and
`DATA_LICENSES.md`.

### 10. The working root can move

`OPENPLACES_ROOT` overrides the checkout as the root for `data/` and `config/`.
Every pipeline function already takes `root` as an argument, so nothing in the
core modules had to change.


### 11. CI stays single

The migration updates the repository's existing `.github/workflows/tests.yml`
instead of adding a second overlapping workflow. It tests Python 3.11 and 3.12,
keeps Osmium installed so OSM tests do not silently skip, and smoke-tests the
new CLI.

### 12. First OSM run is explicit about disk cost

Before an uncached OSM build, the CLI explains that local targets still require
a one-time national Philippines OSM preparation cache. Users who want the
lightest path are told to use `--sources overture`.

### 13. Foursquare distinguishes login from approval

The optional Foursquare path now distinguishes a missing package, missing Hugging
Face login, and a logged-in account that lacks approval for the gated dataset.

### 14. Migration edits are AST-bounded where it matters

The migration replaces `source_snapshot`, the Foursquare discovery function, and
the `PAIRS` assignment by their exact Python AST line spans. It refuses if the
expected function, dependency names, or pair value is not present, so nearby
code cannot be deleted by a broad text-region replacement.

### 15. Core source-subset behavior has functional tests

The suite now exercises all seven non-empty source subsets through
`source_snapshot`, dynamic expected edge-shard inventories, and a tiny
`build_observations` run. This tests the behavior introduced by the migration,
not merely the existence of new function parameters.

### 16. Finalize-only runs verify the same tile inventory

The match manifest records both the active source set and the exact effective
source-tile keys. Finalization checks both before reading any match shard. This
matters when a cached Overture land mask appears or changes between sessions: a
finalize-only run cannot silently combine observations from one tile inventory
with edges from another.

### 17. Empty canonical layers remain valid

The core already supports a valid empty GeoParquet output for an area with no
POIs. `openplaces validate` now treats that as a valid zero-row dataset rather
than misreading the absent `min(source_count)` as a source-count failure.

## Known gaps left open

- `--sources osm` alone produces no land mask, so tile coverage extends over
  water. `run.json` records this; the pipeline does not yet offer an
  Overture-independent boundary.
