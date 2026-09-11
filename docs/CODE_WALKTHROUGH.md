# OpenPlaces PH code walkthrough

This is the “read the project without already being a programmer” guide.

The important idea is that OpenPlaces PH is not one giant script. It is a chain of small, restartable stages. Most functions either:

1. decide **what work belongs in a small tile**;
2. ask DuckDB/Osmium to do that work without loading everything into Python; or
3. write a durable checkpoint so the work does not need to be repeated.

## Start here: `run.py`

`run.py` is intentionally tiny. It simply calls the command-line program in `src/openplaces_ph/cli.py`.

This means these two commands are conceptually the same:

```powershell
python run.py --status
openplaces --status
```

The second form works after `pip install -e .`.

---

## `cli.py`: the conductor

`cli.py` does not do the heavy data processing itself. It decides which stage should run and passes the right paths and settings to the specialized modules.

Read `main()` from top to bottom as a flowchart:

```text
parse command-line options
        |
validate options
        |
resolve Philippines / named test-area scope
        |
choose temp directory and memory limits
        |
if requested: prepare sources
        |
if requested: match source pairs
        |
if requested: finalize canonical POIs
```

The three most useful switches are:

```text
--only sources
--only match
--only finalize
```

They exist so a long run can be treated as three separate jobs.

### Why worker counts look low

The defaults are not trying to maximize benchmark throughput on a server. They are trying to avoid turning an 8 GB Windows laptop into a paging machine.

Foursquare gets two workers because network waiting can overlap. Matching stays at one worker because it is CPU/disk intensive and every extra process would receive its own DuckDB memory allowance.

---

## `config.py`: what geographic area are we doing?

A `Scope` is just a name plus one or more bounding boxes.

For the national run:

```text
scope.name = "philippines"
scope.bboxes = (Philippine bounding box,)
```

For a smoke test such as Metro Manila, `config/areas.yml` supplies a smaller box.

`bbox_sql()` converts one or more bounding boxes into a SQL condition such as:

```sql
(lon >= 120.85 AND lon < 121.20 AND lat >= 14.30 AND lat < 14.90)
```

Notice the `<` on east/north. Tiles use half-open boundaries so a point exactly on a tile edge belongs to one tile rather than being duplicated into two.

---

## `tiles.py`: divide the map into manageable rectangles

The project uses two tile scales by default:

```text
1.00 degree  -> source acquisition/checkpoint tile
0.25 degree  -> matching checkpoint tile
```

A `Tile` knows its west, south, east, and north edges.

The functions here answer simple geometric bookkeeping questions:

- Which source tiles intersect this bounding box?
- Which 0.25-degree child tiles lie inside a 1-degree tile?
- What bounding box do we get after adding a 120 m halo?

The halo matters because a place just outside the current match tile may still be the correct match for a place just inside it.

---

## `db.py`: stop DuckDB from taking over the computer

DuckDB is extremely capable, but its normal defaults assume it may use a large share of the machine.

Every important connection goes through `connect()`.

That helper sets:

```text
memory_limit
threads
temp_directory
preserve_insertion_order = false
small write buffers
```

The temporary directory is important. If a sort or join cannot fit inside the allowed memory, DuckDB can spill intermediate data there instead of simply failing.

`spatial=True` loads geometry functions only for stages that need them.

`httpfs=True` loads remote-file support and adds retry settings for network Parquet reads.

---

## `util.py`: small reliability tools

This module contains boring-looking functions that make the whole project safer.

### `atomic_json()`

Writes to `file.part`, then renames it to the real filename. A crash therefore leaves either the old valid file or a temporary file, not half a JSON document pretending to be complete.

### `valid_parquet()`

Opens the Parquet footer. A file merely existing is not enough to count as a checkpoint.

### `resume_download()`

Downloads a large file into `*.part` and resumes from the existing byte count if the server supports HTTP Range requests.

A small sidecar, `*.part.meta.json`, records which upstream version (ETag or Last-Modified) the partial bytes came from. A resume sends that version in an `If-Range` header: the server continues only if its file is still the same, and otherwise sends the whole new file. Geofabrik replaces its extract every day, so without this a resume on a later day would join the start of one file to the end of another.

Before promotion it checks the expected byte length. This prevents a truncated national OSM file from becoming a permanent “successful” cache entry.

### `normalize_name()`

This is the human-readable Python version of name normalization used by tests and diagnostics. National processing performs the same normalization inside DuckDB rather than calling Python once per row, and `test_normalization_parity.py` checks that the two agree character by character.

Note what the normalization removes: any letter that does not reduce to plain ASCII after accents are stripped. A name written only in Chinese, Korean, or Arabic script becomes an empty string, and such a record is not included in the output.

---

## `snapshot.py`: which source vintage does each checkpoint belong to?

Every later checkpoint is only valid for the exact source data it was computed from. This module records that relation:

- `bind_dir_to_release()` makes a normalized FSQ/Overture directory hold tiles of one release only;
- `source_snapshot()` checks that all three sources are complete for the requested tiles and returns one small dictionary describing them;
- `discard_dir()` removes a directory by renaming it first, so a crash can never leave a half-deleted directory that still looks valid.

Matching stores the snapshot beside its shards; finalization compares it with the current sources before it starts.

---

## `sources.py`: turn three very different providers into the same simple schema

The goal of source preparation is to make every provider eventually look like:

```text
source
source_id
name
category
lon
lat
provenance
upstream_license
name_norm
name_tokens
```

After that, matching does not need to know the original source format.

### OpenStreetMap path

```text
Geofabrik national PBF
        |
osmium tags-filter
        |
smaller POI PBF
        |
osmium export
        |
GeoJSONSeq
        |
DuckDB ST_Read
        |
ST_PointOnSurface
        |
partitioned Parquet source tiles
```

Osmium does the large PBF work because it is designed for streaming OSM data. Python does not parse the national PBF into objects.

### Overture path

For each source tile:

```text
pinned release
      |
stream tile with official Overture reader
      |
raw GeoParquet checkpoint
      |
normalize fields/names
      |
check against Philippine land geometry + tolerance
      |
normalized Parquet tile
```

The country boundary is also from the pinned Overture release.

The code explicitly assigns `OGC:CRS84` to geometry operations. This means X=longitude and Y=latitude is not merely assumed in a comment; it is attached to the geometry type.

### Foursquare path

DuckDB queries Hugging Face Parquet directly over HTTP.

The important filters are pushed into that remote query:

```text
country = PH
not closed
inside current tile
inside current requested scope
```

The result is written immediately to one local tile checkpoint.

---

## `matching.py`: decide which records might represent the same place

This is the heart of entity resolution.

### Step 1: spatial blocking

Comparing every FSQ point with every Overture point would be impossible.

Instead, each coordinate is converted to a small integer grid cell:

```text
gx = floor(lon / grid_size)
gy = floor(lat / grid_size)
```

A point is compared only with records in its own cell and the eight neighboring cells.

The grid size is derived from `max_distance_m`. This is important: if the search radius grows, the blocking grid grows too, so the optimization cannot silently hide valid candidates.

### Step 2: cheap rectangle test

The 3×3 grid neighborhood is still bigger than the actual search circle.

Before computing Haversine distance, the SQL checks whether longitude and latitude differences are even capable of being within range.

Simple subtraction is much cheaper than repeated sine/cosine calculations.

### Step 3: exact distance

Haversine distance is computed for survivors.

Anything beyond the configured radius disappears.

### Step 4: name similarity

The code compares:

```text
normalized name
sorted normalized name tokens
```

Using sorted tokens makes these more similar:

```text
SM North Starbucks
Starbucks SM North
```

### Step 5: acceptance threshold

Distance and name similarity determine whether the pair becomes an accepted edge.

The threshold ladder lives in exactly one function: `acceptance_bands()`.

A crucial detail is that reducing the radius only truncates the ladder. It does not rewrite the calibration of the remaining distance interval.

### Step 6: ranking score

Accepted links receive a score composed mostly of name similarity, with distance and category as supporting terms.

This score does **not** decide whether a link exists; threshold acceptance happens first. The score decides which accepted links get first chance during constrained clustering.

### Checkpointing

Every:

```text
0.25-degree core tile × source pair
```

becomes one independent Parquet file.

If matching stops halfway through the Philippines, those completed files remain valid. `_config.json` records the matching settings and the source snapshot; if either changes, the shards are rebuilt. `_COMPLETE.json` is written only after every expected shard exists, and finalization will not start without it.

---

## `finalize.py`: turn a graph of accepted links into canonical entities

This is the most subtle module. Read it as six stages.

### Stage A: `build_observations()`

Concatenates normalized FSQ/Overture/OSM records into one table and assigns a dense integer `row_id`.

Why integers? Comparing and sorting integer IDs is much cheaper than carrying long source strings everywhere.

The ID is assigned in a streaming second pass using Parquet's `file_row_number` rather than a national SQL window.

### Stage B: `build_edge_ids()`

Every accepted source edge originally says something like:

```text
fsq:f123 <-> osm:node/456
```

This stage attaches the corresponding dense IDs:

```text
123456 <-> 765432
```

It then verifies that the number of resolved edges is **exactly** the number of source edges. Too few means something was missing; too many usually means a supposedly unique source ID was duplicated.

### Stage C: `build_ranked_pairs()`

This is the one unavoidable national sort.

The clever part is what gets sorted:

```text
left_id
right_id
```

Only two integers are written into the sorted result. The wide edge fields remain in the unsorted file.

### Stage D: `cluster_edges()`

This is greedy union-find.

Imagine every observation begins alone:

```text
FSQ-A       Overture-A       OSM-A
```

The strongest edge arrives first. If joining the two components would not create two records from the same source, they merge.

The component source mask uses bits:

```text
FSQ      001
Overture 010
OSM      100
```

So a cluster containing FSQ+OSM has mask `101`. Trying to merge it with another FSQ record (`001`) gives:

```text
101 & 001 = 001
```

which is non-zero, so the merge is rejected.

#### Why `array('I')`?

A Python list of millions of integers is deceptively expensive because it stores pointers to Python integer objects.

The real national state instead uses:

```text
parent -> array('I')  -> 4 bytes each
rank   -> bytearray   -> 1 byte each
mask   -> bytearray   -> 1 byte each
```

That is approximately 6 bytes per observation before small container overhead.

#### Why snapshots instead of a memory-mapped state file?

A directly modified memmap can be flushed by the operating system at times the program did not intend. After a power loss, the data and the “I finished row group X” marker could disagree.

Instead, OpenPlaces writes a brand-new checkpoint, fsyncs it, then atomically replaces the old checkpoint. The old committed state is never modified in place.

### Stage E: `build_match_edges()`

Joins the cluster results back onto every accepted edge and records:

```text
same_cluster_final
```

An edge may have passed the threshold but still fail to survive final clustering because accepting it would violate the one-record-per-source rule.

### Stage F: `build_canonical()`

Groups the observations by final cluster and writes the public point layer.

It keeps source-specific fields rather than throwing them away.

It also computes:

```text
source_count
known_independent_source_count
match_score_min
cluster_max_pair_distance_m
completed_transitively
```

The final point geometry is explicitly assigned `OGC:CRS84`.

Before promotion, the Parquet footer must contain the standard GeoParquet `geo` metadata key.

---

## `tests/`: executable documentation

The tests are worth reading because each file isolates one promise made by the project.

- `test_grid_coverage.py` — spatial blocking cannot hide an in-range pair.
- `test_threshold_parity.py` — SQL and Python acceptance logic agree.
- `test_matching.py` — named threshold examples and source-duplication invariant.
- `test_cluster_edges.py` — strongest-first union-find and exact resume behavior.
- `test_finalize_pipeline.py` — tiny end-to-end finalization, including transitivity and GeoParquet output.
- `test_imports.py` — cheap CLI imports do not drag in acquisition clients.
- `test_tiles.py` — tile arithmetic and halos.
- `test_match_sql.py` — the real matching SQL finds exactly the pairs an all-pairs search finds, and refuses missing source tiles.
- `test_snapshot.py` — stop/resume and refresh sequences across sessions never mix source vintages.
- `test_download.py` — byte-range resume against a local server that changes its file between requests.
- `test_overture_normalize.py` / `test_osm_prepare.py` — source normalization on local data (the OSM test needs Osmium).
- `test_provenance.py` — Overture licence labels from provider dataset names.
- `test_normalization_parity.py` — SQL and Python name normalization agree.

A test failure is preferable to a quiet change in the public data contract.

---

## If you want to modify the project

The safest order is:

1. change one rule;
2. add or update the smallest test that expresses that rule;
3. run `pytest -q`;
4. run the Metro Manila smoke test;
5. inspect the output in QGIS;
6. only then run the whole Philippines.

For matching changes, do not judge correctness only by “the program runs.” The dangerous bugs are the ones where an optimization quietly changes which candidate pairs are even visible to the model.
