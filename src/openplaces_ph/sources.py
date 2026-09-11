"""Acquire and normalize the three POI sources.

Design goals for a resource-constrained laptop
------------------------------
1. Never download a worldwide dataset when a Philippine subset can be queried.
2. Keep remote work in 1-degree blocks, so a failed request loses one block.
3. Normalize names *once* here. National matching then compares precomputed
   strings in DuckDB instead of repeatedly calling Python fuzzy functions.
4. Write every completed block atomically to Parquet. A normalized source
   directory is bound to one pinned release (``_release.json``); a block is
   downloaded again only when that release changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# NOTE: ``huggingface_hub`` and ``overturemaps`` are imported lazily inside the
# functions that actually acquire data. Importing them at module scope made
# ``openplaces --status`` and the pure-logic unit tests depend on the full
# network client stack for no reason.
from functools import lru_cache

from .config import Scope, bbox_sql
from .db import connect
from .snapshot import adopt_unbound_dir, bind_dir_to_release, discard_dir, pinned_release
from .tiles import Tile, tiles_for_bboxes
from .util import (
    atomic_json,
    check_commands,
    download_identity,
    read_json,
    resume_download,
    run_command,
    valid_parquet,
)

GEOFABRIK_PBF = "https://download.geofabrik.de/asia/philippines-latest.osm.pbf"
FSQ_REPO = "foursquare/fsq-os-places"
OVERTURE_STAC = "https://stac.overturemaps.org/catalog.json"

# Broad rectangle used only for initial remote acquisition. OSM itself comes
# from Geofabrik's Philippine extract; Overture is subsequently clipped to the
# Overture country land polygon; FSQ is filtered with country='PH'.
PH_BBOX = (116.80, 4.40, 126.70, 21.30)

# Overture/GeoJSON coordinates are longitude, latitude in WGS 84. DuckDB 1.5
# can carry CRS information in the GEOMETRY type itself; making it explicit
# prevents accidental mixing of "geometry with CRS" and "geometry without CRS"
# after a future library upgrade. OGC:CRS84 uses the familiar X=longitude,
# Y=latitude axis order.
CRS84 = "OGC:CRS84"

# OSM keys that strongly suggest that an object is a POI/establishment. We keep
# named objects carrying any of these keys. This is intentionally broader than
# shop/amenity because offices, workshops, tourism establishments, etc. matter
# for later industry classification.
OSM_KEYS = (
    "amenity",
    "shop",
    "office",
    "craft",
    "tourism",
    "leisure",
    "healthcare",
    "industrial",
    "man_made",
    "club",
    "emergency",
    "public_transport",
)

# SPDX-style identifiers used in the normalized observation layer.  These are
# data licenses, not the MIT license covering OpenPlaces PH's source code.
# Overture Places are the only source clipped by geometry (Foursquare uses
# ``country = 'PH'`` and OSM inherits the Geofabrik extract). A hard
# point-in-polygon test therefore drops coastal, reclaimed-land, port, and
# small-island establishments from Overture *only*, which biases source_count
# and evidence_tier downwards exactly along the coastline. This tolerance
# (~0.002 deg, roughly 220 m) restores that band. Ocean-only tiles are already
# removed upstream by ``filter_tiles_to_boundary``.
OVERTURE_LAND_TOLERANCE_DEG = 0.002

# The overturemaps bbox filter uses strict inequalities (row xmax > query xmin
# and row xmin < query xmax). A point lying exactly on a tile edge therefore
# matched *neither* neighbouring tile and was lost. Downloads are widened by a
# negligible margin (~0.1 m); the half-open tile predicate applied afterwards
# still assigns every point to exactly one tile.
OVERTURE_BBOX_EDGE_EPS_DEG = 1e-6

FSQ_LICENSE = "Apache-2.0"
OSM_LICENSE = "ODbL-1.0"
OVERTURE_APACHE_LICENSE = "Apache-2.0"
OVERTURE_CC0_LICENSE = "CC0-1.0"
OVERTURE_CDLA_LICENSE = "CDLA-Permissive-2.0"


# Regular expression for "this Overture record declares Foursquare provenance",
# applied to lower-cased dataset names only. 0.2.0 wrote it with doubled
# backslashes, so the SQL contained ``\\bfsq\\b``: RE2 reads that as a literal
# backslash followed by "b", and the ``fsq`` alternative could never match.
FSQ_PROVENANCE_PATTERN = r"foursquare|\bfsq\b"

# DuckDB serializes the Overture ``sources`` list of structs as, for example,
# ``[{'property': '', 'dataset': Foursquare, 'record_id': 4b05...}]``.
# Group 1 captures one dataset value: either a quoted string or an unquoted run.
# (SQL literal; '' is an escaped quote and \\ is one literal backslash for RE2.)
_DATASET_VALUE_REGEX = r"'''dataset'':\s*(''(?:[^''\\]|\\.)*''|[^,}]*)'"


def overture_datasets_sql(provenance_column: str = "provenance") -> str:
    """DuckDB expression: lower-cased Overture dataset names, joined by ``|``.

    Provider patterns used to be matched against the *whole* serialized
    ``sources`` text, which also contains record ids and timestamps. A
    Foursquare record id containing ``dac`` (about 0.5% of 24-hex ids) was
    therefore classified as MIXED/REVIEW, and an unknown future provider could
    be labelled CDLA because of characters in its record id. If no ``dataset``
    key can be parsed, the whole text is used, i.e. the old behaviour.
    """
    text = f"coalesce(CAST({provenance_column} AS VARCHAR), '')"
    found = f"regexp_extract_all({text}, {_DATASET_VALUE_REGEX}, 1)"
    return (
        f"(CASE WHEN len({found}) > 0 "
        f"THEN '|' || lower(replace(array_to_string({found}, '|'), '''', '')) || '|' "
        f"ELSE lower({text}) END)"
    )


def overture_fsq_provenance_sql(provenance_column: str = "provenance") -> str:
    """DuckDB boolean: the Overture record declares Foursquare provenance."""
    return (
        f"regexp_matches({overture_datasets_sql(provenance_column)}, "
        f"'{FSQ_PROVENANCE_PATTERN}')"
    )


def overture_license_sql(provenance_column: str = "provenance") -> str:
    """Return a DuckDB CASE expression for current Overture Places licenses.

    Overture Places is multi-license at the upstream-provider level.  We infer
    the applicable license only from provider provenance that Overture itself
    exposes.  Unknown or mixed future providers are deliberately marked for
    review rather than silently assigned a permissive license. Patterns are
    matched against dataset names only (see :func:`overture_datasets_sql`).
    """
    d = overture_datasets_sql(provenance_column)
    is_fsq = f"regexp_matches({d}, '{FSQ_PROVENANCE_PATTERN}')"
    is_atp = f"regexp_matches({d}, 'alltheplaces|all_the_places')"
    is_cdla = (
        f"regexp_matches({d}, 'meta|microsoft|pinmeto|krick|renderseo|"
        "brightquery|dac')"
    )
    return (
        "CASE "
        f"WHEN ({is_fsq} AND ({is_atp} OR {is_cdla})) "
        f"  OR ({is_atp} AND {is_cdla}) THEN 'MIXED/REVIEW' "
        f"WHEN {is_fsq} THEN '{OVERTURE_APACHE_LICENSE}' "
        f"WHEN {is_atp} THEN '{OVERTURE_CC0_LICENSE}' "
        f"WHEN {is_cdla} THEN '{OVERTURE_CDLA_LICENSE}' "
        "ELSE 'UNKNOWN' END"
    )


# Backwards-compatible private name used by 0.2.0 callers.
_overture_license_sql = overture_license_sql


def check_external_tools() -> None:
    """Fail early if the Conda-installed Osmium CLI is not visible on PATH."""
    check_commands(("osmium",))


def _sql_name_norm(column: str) -> str:
    """DuckDB expression for deterministic, inexpensive name normalization.

    We preserve the original name separately. The normalized form is only for
    entity resolution. ``strip_accents`` and lower-casing handle common spelling
    variants, while punctuation is reduced to spaces.
    """
    return (
        "trim(regexp_replace("
        f"lower(strip_accents(replace(CAST({column} AS VARCHAR), '&', ' and '))), "
        "'[^[:alnum:]]+', ' ', 'g'))"
    )


def _tile_sql(lon: str, lat: str, tile: Tile) -> str:
    return (
        f"({lon} >= {tile.west} AND {lon} < {tile.east} "
        f"AND {lat} >= {tile.south} AND {lat} < {tile.north})"
    )


def _run_tiles(worker_fn, tiles: list[Tile], workers: int, *args) -> None:
    """Run independent source blocks with conservative parallelism.

    One future == one 1-degree tile. This matters for Ctrl+C: Python cannot
    reliably cancel a subprocess already doing an Overture download, so the
    maximum unavoidable unit of unfinished work is deliberately small.
    """
    if not tiles:
        return
    workers = max(1, min(workers, len(tiles)))
    if workers == 1:
        for tile in tiles:
            worker_fn(tile, *args)
        return

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker_fn, tile, *args) for tile in tiles]
        try:
            for future in as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            raise


# ---------------------------------------------------------------------------
# OSM
# ---------------------------------------------------------------------------

def _osmium_export_config(path: Path) -> None:
    """Tell osmium-export exactly which attributes/tags we need downstream."""
    payload = {
        "attributes": {"type": "osm_type", "id": "osm_id"},
        "linear_tags": False,
        "area_tags": list(OSM_KEYS),
        "include_tags": [
            "name",
            "name:en",
            "brand",
            "operator",
            *OSM_KEYS,
            "cuisine",
            "addr:housenumber",
            "addr:street",
            "addr:city",
        ],
        "format_options": {"print_record_separator": False},
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _identity_tag(identity: dict) -> str:
    """Short, filesystem-safe fingerprint of one downloaded PBF version."""
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def prepare_osm(
    root: Path,
    source_tile_deg: float,
    temp_dir: Path,
    memory_limit: str,
    *,
    refresh: bool = False,
    keep_intermediates: bool = False,
) -> Path:
    """Prepare named OSM POIs as partitioned Parquet.

    Durable boundaries are intentionally coarse:
      A. byte-resumable national PBF download (If-Range protected)
      B. POI-tag-filtered PBF
      C. GeoJSONSeq geometry export
      D. final partitioned Parquet dataset

    B and C are named after the identity of the PBF they came from, and D
    records that identity in ``_SUCCESS.json``. Downstream stages use it to
    know which OSM snapshot they were built from.

    ``refresh`` writes a marker that stays until new tiles are published, so
    an interrupted refresh continues on the next run with or without the flag
    (the same behaviour as a re-pinned FSQ/Overture release). If the upstream
    extract has not changed, nothing is downloaded or rebuilt.
    """
    cache = root / "data" / "cache" / "osm"
    work = root / "data" / "work" / "osm"
    cache.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    tag = str(source_tile_deg).replace(".", "p")
    tiles_dir = cache / f"tiles_{tag}deg"
    success = tiles_dir / "_SUCCESS.json"
    pending = cache / "_REFRESH_PENDING.json"

    if refresh:
        atomic_json(pending, {"reason": "--refresh-sources"})
    refreshing = pending.exists()

    if success.exists() and not refreshing:
        print("[OSM] normalized cache ready")
        return tiles_dir

    pbf = resume_download(
        GEOFABRIK_PBF,
        cache / "philippines-latest.osm.pbf",
        refresh=refreshing,
    )
    identity = download_identity(pbf)
    current = read_json(success)
    if current is not None and current.get("pbf") == identity:
        pending.unlink(missing_ok=True)
        print("[OSM] normalized cache already matches the current extract")
        return tiles_dir

    id_tag = _identity_tag(identity)
    poi_pbf = work / f"osm-poi-{id_tag}.osm.pbf"
    seq = work / f"osm-poi-{id_tag}.geojsonseq"
    # Intermediates of another PBF version (or 0.2.0's unversioned names)
    # can never be reused for this one; remove them to free disk space.
    for stale in work.glob("osm-poi*"):
        if id_tag not in stale.name:
            stale.unlink(missing_ok=True)

    if not poi_pbf.exists():
        tmp = poi_pbf.with_name(poi_pbf.name + ".part")
        tmp.unlink(missing_ok=True)
        filters = [f"nwr/{key}" for key in OSM_KEYS]
        # We do NOT use --omit-referenced here: polygon geometries need their
        # referenced nodes in order to export valid representative points.
        # ``-f pbf`` is required: Osmium detects the output format from the
        # file name, and a name ending in ".part" made 0.2.0 stop here with
        # "Could not detect file format".
        run_command(["osmium", "tags-filter", "-t", "-f", "pbf", "-o", tmp, "-O", pbf, *filters])
        os.replace(tmp, poi_pbf)
    else:
        print(f"[cache] {poi_pbf}")

    if not seq.exists():
        cfg = work / "osmium-export.json"
        _osmium_export_config(cfg)
        tmp = seq.with_name(seq.name + ".part")
        tmp.unlink(missing_ok=True)
        run_command(["osmium", "export", "-f", "geojsonseq", "-c", cfg, "-o", tmp, "-O", poi_pbf])
        os.replace(tmp, seq)
    else:
        print(f"[cache] {seq}")

    success_payload = {"source_tile_deg": source_tile_deg, "pbf": identity}
    tmp_tiles = tiles_dir.with_name(tiles_dir.name + ".part")
    if (read_json(tmp_tiles / "_SUCCESS.json") or {}) != success_payload:
        if tmp_tiles.exists():
            shutil.rmtree(tmp_tiles)
        tmp_tiles.mkdir(parents=True)
        _partition_osm(seq, tmp_tiles, source_tile_deg, temp_dir, memory_limit)
        atomic_json(tmp_tiles / "_SUCCESS.json", success_payload)

    # Publish: the old tile set stays readable until the new one is complete.
    discard_dir(tiles_dir)
    os.replace(tmp_tiles, tiles_dir)
    pending.unlink(missing_ok=True)

    if not keep_intermediates:
        poi_pbf.unlink(missing_ok=True)
        seq.unlink(missing_ok=True)

    return tiles_dir


def _partition_osm(
    seq: Path,
    out_dir: Path,
    source_tile_deg: float,
    temp_dir: Path,
    memory_limit: str,
) -> None:
    """Stage D: one sequential scan of the GeoJSONSeq into tile partitions."""
    con = connect(temp_dir / "osm_partition", memory_limit=memory_limit, threads=1, spatial=True)

    # DESCRIBE without fetchdf(): keeping pandas out of the pipeline saves RAM.
    columns = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM ST_Read('{seq.as_posix()}')").fetchall()}

    category_parts = []
    for key in OSM_KEYS:
        if key in columns:
            category_parts.append(
                f"CASE WHEN \"{key}\" IS NOT NULL THEN '{key}=' || CAST(\"{key}\" AS VARCHAR) END"
            )
    category_expr = (
        "concat_ws(' | ', " + ", ".join(category_parts) + ")"
        if category_parts else "NULL::VARCHAR"
    )
    name_norm = _sql_name_norm("name")

    # ST_PointOnSurface turns polygons/lines into one representative POI
    # coordinate that is *guaranteed to lie inside the footprint*; a centroid
    # can fall outside concave shapes such as U-shaped malls or ring-shaped
    # markets. It is computed once per row rather than once per axis.
    # We are explicitly building a POINT dataset, so original OSM geometry is
    # not retained here.
    con.execute(
        f"""
        COPY (
            WITH raw AS (
                SELECT
                    'osm'::VARCHAR AS source,
                    CAST(osm_type AS VARCHAR) || '/' || CAST(osm_id AS VARCHAR) AS source_id,
                    CAST(name AS VARCHAR) AS name,
                    {category_expr} AS category,
                    ST_PointOnSurface(geom) AS rep_point,
                    NULL::VARCHAR AS provenance,
                    'ODbL-1.0'::VARCHAR AS upstream_license
                FROM ST_Read('{seq.as_posix()}')
                WHERE name IS NOT NULL AND trim(CAST(name AS VARCHAR)) <> ''
            ), pointed AS (
                SELECT * EXCLUDE (rep_point),
                       ST_X(rep_point)::DOUBLE AS lon,
                       ST_Y(rep_point)::DOUBLE AS lat
                FROM raw
            ), norm AS (
                SELECT *, {name_norm} AS name_norm
                FROM pointed
            ), ready AS (
                SELECT *,
                       array_to_string(list_sort(string_split(name_norm, ' ')), ' ') AS name_tokens,
                       CAST(floor(lon / {source_tile_deg}) AS INTEGER) AS tile_x,
                       CAST(floor(lat / {source_tile_deg}) AS INTEGER) AS tile_y
                FROM norm
                WHERE name_norm <> ''
                  AND lon >= {PH_BBOX[0]} AND lon < {PH_BBOX[2]}
                  AND lat >= {PH_BBOX[1]} AND lat < {PH_BBOX[3]}
            )
            SELECT * FROM ready
        ) TO '{out_dir.as_posix()}' (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            PARTITION_BY (tile_x, tile_y),
            ROW_GROUP_SIZE 25000
        )
        """
    )
    con.close()


@lru_cache(maxsize=4096)
def _osm_tile_files_cached(tiles_dir: Path, ix: int, iy: int) -> tuple[Path, ...]:
    directory = tiles_dir / f"tile_x={ix}" / f"tile_y={iy}"
    return tuple(sorted(directory.glob("*.parquet"))) if directory.exists() else ()


def osm_tile_files(tiles_dir: Path, tile: Tile) -> list[Path]:
    """Resolve the OSM partition files for one source tile.

    Cached: matching resolves the same handful of tiles 48 times per 1-degree
    block (16 child tiles x 3 source pairs), and each uncached call re-globbed
    the directory.
    """
    return list(_osm_tile_files_cached(tiles_dir, tile.ix, tile.iy))


# ---------------------------------------------------------------------------
# Overture
# ---------------------------------------------------------------------------

def resolve_overture_release(root: Path, *, refresh: bool = False) -> str:
    """Pin one Overture release for the entire resumable run.

    The official Overture reader can accept an explicit ``release=`` argument,
    even though the CLI normally follows the STAC catalog's latest release.
    Pinning matters for a long national run: if a monthly Overture release is
    published between two sessions, already-completed and newly-downloaded
    tiles must not silently come from different snapshots.
    """
    cache = root / "data" / "cache" / "overture"
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / "release.json"

    if marker.exists() and not refresh:
        try:
            return json.loads(marker.read_text(encoding="utf-8"))["release"]
        except Exception:
            pass

    # The STAC root is tiny JSON and exposes a machine-readable ``latest`` key.
    response = requests.get(OVERTURE_STAC, timeout=(15, 60))
    response.raise_for_status()
    release = response.json().get("latest")
    if not release:
        raise RuntimeError("Overture STAC did not expose a latest release identifier.")

    atomic_json(marker, {"stac": OVERTURE_STAC, "release": release})
    return str(release)


def pin_overture_release(root: Path, out_dir: Path, *, refresh: bool = False) -> str:
    """Resolve the Overture release for this run, re-pinning it on ``refresh``.

    Tiles written by <= 0.2.0 carry no release manifest. They are recorded
    under the release pinned *before* a refresh can change the pin, so that
    :func:`prepare_overture` can recognise them as old and discard them.
    """
    adopt_unbound_dir(out_dir, pinned_release(root, "overture"), "Overture")
    return resolve_overture_release(root, refresh=refresh)


def _stream_overture_tile(
    overture_type: str,
    bbox: tuple[float, float, float, float],
    release: str,
    target: Path,
    *,
    retries: int = 3,
) -> bool:
    """Stream one pinned Overture bbox to GeoParquet with bounded failure loss.

    Returns ``False`` when STAC says the bbox contains no matching Overture
    files. A partially written ``*.part`` file is never promoted to a durable
    checkpoint.

    Why use the Python streaming API instead of shelling out to the CLI?
    -------------------------------------------------------------------
    The public API accepts ``release=...``. This lets us pin the snapshot while
    still using Overture's STAC file selection, which transfers only Parquet
    fragments intersecting the requested bbox. It also avoids loading the tile
    into GeoPandas or a Python list.
    """
    from overturemaps import record_batch_reader
    from overturemaps.writers import copy as overture_copy, get_writer

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        tmp.unlink(missing_ok=True)
        try:
            reader = record_batch_reader(
                overture_type,
                bbox=bbox,
                release=release,
                connect_timeout=15,
                request_timeout=120,
                stac=True,
            )
            if reader is None:
                return False

            # Use Overture's own GeoParquet writer rather than constructing a
            # table in memory. ``copy`` consumes the RecordBatchReader as a
            # stream and preserves the geometry metadata DuckDB needs later.
            # This is both safer and lower-memory than reader.read_all().
            with get_writer("geoparquet", str(tmp), schema=reader.schema) as writer:
                overture_copy(reader, writer)

            # A non-null reader can still theoretically yield no rows after the
            # bbox filter. The official writer should still create a valid file;
            # if it does not, record this as an empty source tile downstream.
            if not tmp.exists() or tmp.stat().st_size == 0:
                tmp.unlink(missing_ok=True)
                return False

            os.replace(tmp, target)
            return True

        except Exception as exc:
            last_error = exc
            tmp.unlink(missing_ok=True)
            if attempt < retries:
                delay = 5 * attempt
                print(
                    f"[Overture] {overture_type} tile attempt {attempt}/{retries} failed: "
                    f"{exc}. Retrying in {delay}s...",
                    flush=True,
                )
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def _write_empty_source_tile(path: Path, source: str) -> None:
    """Create a valid empty normalized source checkpoint.

    Empty land-edge tiles are normal in an archipelago. Recording them as real
    Parquet files prevents us from repeatedly asking the network for the same
    empty bbox on every resume.
    """
    schema = pa.schema([
        ("source", pa.string()),
        ("source_id", pa.string()),
        ("name", pa.string()),
        ("category", pa.string()),
        ("lon", pa.float64()),
        ("lat", pa.float64()),
        ("provenance", pa.string()),
        ("upstream_license", pa.string()),
        ("name_norm", pa.string()),
        ("name_tokens", pa.string()),
    ])
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.unlink(missing_ok=True)
    arrays = [pa.array([], type=field.type) for field in schema]
    pq.write_table(pa.Table.from_arrays(arrays, schema=schema), tmp, compression="zstd")
    os.replace(tmp, path)


def prepare_overture_boundary(
    root: Path,
    temp_dir: Path,
    memory_limit: str,
    release: str,
    *,
    refresh: bool = False,
) -> Path:
    """Cache one Philippine land polygon from the same pinned Overture release."""
    cache = root / "data" / "cache" / "overture"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"ph_country_boundary_{release}.parquet"

    if valid_parquet(target) and not refresh:
        return target

    raw = cache / f"ph_division_area_raw_{release}.parquet"
    if refresh:
        target.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)

    if not valid_parquet(raw):
        found = _stream_overture_tile("division_area", PH_BBOX, release, raw)
        if not found:
            raise RuntimeError(
                f"Overture release {release} returned no Philippine division-area data."
            )

    tmp = target.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    con = connect(temp_dir / "overture_boundary", memory_limit=memory_limit, threads=1, spatial=True)
    con.execute(
        f"""
        COPY (
            SELECT ST_MemUnion_Agg(
                       ST_SetCRS(geometry::GEOMETRY, '{CRS84}')
                   ) AS geometry
            FROM read_parquet('{raw.as_posix()}')
            WHERE country = 'PH' AND subtype = 'country' AND is_land = true
        ) TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    con.close()
    os.replace(tmp, target)
    raw.unlink(missing_ok=True)
    return target


def _overture_worker(
    tile: Tile,
    root_s: str,
    scope_bboxes: tuple[tuple[float, float, float, float], ...],
    scope_slug: str,
    release: str,
    boundary_s: str,
    temp_s: str,
    memory_limit: str,
    keep_raw: bool,
) -> None:
    root = Path(root_s)
    boundary = Path(boundary_s)
    temp_root = Path(temp_s)
    out_dir = root / "data" / "sources" / scope_slug / "overture"
    raw_dir = root / "data" / "work" / scope_slug / "overture_raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    # ``out_dir`` is bound to ``release`` by prepare_overture(), so a valid
    # file here is always a tile of the pinned release.
    target = out_dir / f"{tile.key}.parquet"
    if valid_parquet(target):
        return
    target.unlink(missing_ok=True)

    raw = raw_dir / f"{tile.key}.{release}.parquet"

    # One 1-degree bbox == one durable network unit. Overture's public reader
    # uses STAC to select only intersecting source Parquet fragments and streams
    # batches from S3. The explicit release keeps all resumed tiles consistent.
    if not valid_parquet(raw):
        eps = OVERTURE_BBOX_EDGE_EPS_DEG
        download_bbox = (tile.west - eps, tile.south - eps, tile.east + eps, tile.north + eps)
        found = _stream_overture_tile("place", download_bbox, release, raw)
        if not found:
            _write_empty_source_tile(target, "overture")
            return

    con = connect(temp_root / f"ov_{tile.key}", memory_limit=memory_limit, threads=1, spatial=True)
    columns = {row[0] for row in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{raw.as_posix()}')").fetchall()}

    categories = []
    if "taxonomy" in columns:
        categories.append("taxonomy.primary")
    if "basic_category" in columns:
        categories.append("basic_category")
    if "categories" in columns:
        categories.append("categories.primary")
    category_expr = "COALESCE(" + ", ".join(categories + ["NULL::VARCHAR"]) + ")" if categories else "NULL::VARCHAR"
    provenance_expr = "CAST(sources AS VARCHAR)" if "sources" in columns else "NULL::VARCHAR"
    overture_license_expr = overture_license_sql("provenance")
    status_pred = (
        "AND (operating_status IS NULL OR operating_status <> 'permanently_closed')"
        if "operating_status" in columns else ""
    )
    scope_pred = bbox_sql("lon", "lat", scope_bboxes)
    tile_pred = _tile_sql("lon", "lat", tile)
    name_norm = _sql_name_norm("name")
    tol = OVERTURE_LAND_TOLERANCE_DEG
    tile_env = (
        tile.west - tol, tile.south - tol, tile.east + tol, tile.north + tol,
    )

    tmp = target.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    con.execute(
        f"""
        COPY (
            WITH raw0 AS (
                SELECT
                    'overture'::VARCHAR AS source,
                    CAST(id AS VARCHAR) AS source_id,
                    CAST(names.primary AS VARCHAR) AS name,
                    CAST({category_expr} AS VARCHAR) AS category,
                    ST_X(geometry)::DOUBLE AS lon,
                    ST_Y(geometry)::DOUBLE AS lat,
                    {provenance_expr} AS provenance,
                    ST_SetCRS(geometry::GEOMETRY, '{CRS84}') AS geometry
                FROM read_parquet('{raw.as_posix()}')
                WHERE names.primary IS NOT NULL {status_pred}
            ), norm AS (
                SELECT *, {name_norm} AS name_norm
                FROM raw0
            ), ready AS (
                SELECT source, source_id, name, category, lon, lat, provenance,
                       {overture_license_expr} AS upstream_license,
                       name_norm,
                       array_to_string(list_sort(string_split(name_norm, ' ')), ' ') AS name_tokens,
                       geometry
                FROM norm
                WHERE name_norm <> ''
            )
            , land AS (
                -- Clip the national multipolygon (thousands of islands) once
                -- to this tile before testing any point against it. Testing
                -- every point against the whole archipelago geometry is the
                -- single most expensive predicate in source preparation.
                SELECT ST_Intersection(
                           b.geometry,
                           ST_SetCRS(
                               ST_MakeEnvelope({tile_env[0]}, {tile_env[1]},
                                               {tile_env[2]}, {tile_env[3]}),
                               '{CRS84}'
                           )
                       ) AS geometry
                FROM read_parquet('{boundary.as_posix()}') b
            )
            SELECT source, source_id, name, category, lon, lat, provenance, upstream_license,
                   name_norm, name_tokens
            FROM ready, land
            WHERE {tile_pred}
              AND {scope_pred}
              AND ST_DWithin(ready.geometry, land.geometry, {OVERTURE_LAND_TOLERANCE_DEG})
            ORDER BY lon, lat
        ) TO '{tmp.as_posix()}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 25000
        )
        """
    )
    con.close()
    os.replace(tmp, target)

    if not keep_raw:
        raw.unlink(missing_ok=True)


def prepare_overture(
    root: Path,
    scope: Scope,
    source_tiles: list[Tile],
    release: str,
    boundary: Path,
    workers: int,
    temp_dir: Path,
    memory_limit: str,
    *,
    keep_raw: bool = False,
) -> Path:
    """Normalize every missing Overture block of ``release``.

    The output directory is bound to ``release`` before any block is written:
    if it holds tiles of another release, they are discarded first. Tiles of
    two releases can therefore never be mixed, even after an interrupted
    refresh, and an interrupted refresh continues instead of restarting.
    """
    out_dir = root / "data" / "sources" / scope.slug / "overture"
    bind_dir_to_release(out_dir, release, "Overture")
    todo = [t for t in source_tiles if not valid_parquet(out_dir / f"{t.key}.parquet")]
    print(f"[Overture] pinned release {release}")
    print(f"[Overture] {len(source_tiles) - len(todo)}/{len(source_tiles)} blocks cached")

    _run_tiles(
        _overture_worker, todo, workers,
        str(root), scope.bboxes, scope.slug, release, str(boundary), str(temp_dir),
        memory_limit, keep_raw,
    )
    return out_dir


# ---------------------------------------------------------------------------
# Foursquare OS Places
# ---------------------------------------------------------------------------

def _discover_fsq_release() -> str:
    """Find the newest release folder without listing every global Parquet file."""
    from huggingface_hub import HfApi, get_token

    if not get_token():
        raise RuntimeError(
            "Foursquare requires one-time Hugging Face authorization. "
            "Accept access to foursquare/fsq-os-places, then run: hf auth login"
        )

    api = HfApi()
    dates: set[str] = set()

    # Non-recursive tree listing is important: recursively enumerating a huge
    # worldwide Parquet repository is unnecessary overhead on every restart.
    for item in api.list_repo_tree(
        repo_id=FSQ_REPO,
        repo_type="dataset",
        path_in_repo="release",
        recursive=False,
    ):
        path = getattr(item, "path", "")
        match = re.search(r"release/dt=(\d{4}-\d{2}-\d{2})/?$", path)
        if match:
            dates.add(match.group(1))

    if not dates:
        # Compatibility fallback for a Hub repository whose directory tree is
        # not exposed as folders by a future client/server version. This path is
        # slower because it enumerates filenames, so it is used only if needed.
        for filename in api.list_repo_files(repo_id=FSQ_REPO, repo_type="dataset"):
            match = re.search(r"release/dt=(\d{4}-\d{2}-\d{2})/places/parquet/", filename)
            if match:
                dates.add(match.group(1))

    if not dates:
        raise RuntimeError("Could not discover a Foursquare release folder on Hugging Face.")
    return max(dates)


def resolve_fsq_release(root: Path, *, refresh: bool = False) -> str:
    """Pin one FSQ release for an entire resumable run.

    This is both faster and reproducible. A run resumed tomorrow should not mix
    yesterday's already-cached tiles with a newly published release.
    ``--refresh-sources`` deliberately discovers and pins a fresh release.
    """
    cache = root / "data" / "cache" / "foursquare"
    cache.mkdir(parents=True, exist_ok=True)
    marker = cache / "release.json"

    if marker.exists() and not refresh:
        try:
            return json.loads(marker.read_text(encoding="utf-8"))["release"]
        except Exception:
            pass

    release = _discover_fsq_release()
    atomic_json(marker, {"repo": FSQ_REPO, "release": release})
    return release


def _fsq_worker(
    tile: Tile,
    root_s: str,
    scope_bboxes: tuple[tuple[float, float, float, float], ...],
    scope_slug: str,
    release: str,
    temp_s: str,
    memory_limit: str,
    retries: int = 4,
) -> None:
    """Query one FSQ 1-degree block and commit it atomically.

    Each retry creates a *fresh* DuckDB connection. This is intentional: after
    a remote HTTP/Arrow failure, reusing the same analytical connection can
    retain failed state or cached handles. A fresh 512-MB connection is cheap
    compared with rerunning hours of national acquisition.
    """
    root = Path(root_s)
    out_dir = root / "data" / "sources" / scope_slug / "fsq"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{tile.key}.parquet"

    # ``out_dir`` is bound to ``release`` by prepare_foursquare().
    if valid_parquet(target):
        return
    target.unlink(missing_ok=True)

    remote = f"hf://datasets/{FSQ_REPO}/release/dt={release}/places/parquet/*.parquet"
    tile_pred = _tile_sql("longitude", "latitude", tile)
    scope_pred = bbox_sql("longitude", "latitude", scope_bboxes)
    name_norm = _sql_name_norm("name")
    tmp = target.with_suffix(".parquet.part")

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        tmp.unlink(missing_ok=True)
        con = None
        try:
            con = connect(
                Path(temp_s) / f"fsq_{os.getpid()}",
                memory_limit=memory_limit,
                threads=1,
                httpfs=True,
            )
            try:
                # Hugging Face stores the token outside the repository after
                # ``hf auth login``. DuckDB's credential chain reads it without
                # copying a secret into code or Git history.
                con.execute(
                    "CREATE SECRET hf_token "
                    "(TYPE HUGGINGFACE, PROVIDER credential_chain)"
                )
            except duckdb.Error:
                # It may already exist in the connection/extension context.
                pass

            # Country + bbox predicates are deliberately both present. Country
            # provides semantic correctness; the coordinate predicates enable
            # Parquet row-group/file pruning and reduce remote bytes read.
            # Only columns used downstream are materialized locally.
            con.execute(
                f"""
                COPY (
                    WITH raw AS (
                        SELECT
                            'fsq'::VARCHAR AS source,
                            CAST(fsq_place_id AS VARCHAR) AS source_id,
                            CAST(name AS VARCHAR) AS name,
                            CAST(fsq_category_labels AS VARCHAR) AS category,
                            CAST(longitude AS DOUBLE) AS lon,
                            CAST(latitude AS DOUBLE) AS lat,
                            NULL::VARCHAR AS provenance,
                            'Apache-2.0'::VARCHAR AS upstream_license
                        FROM read_parquet('{remote}', union_by_name=true)
                        WHERE country = 'PH'
                          AND date_closed IS NULL
                          AND name IS NOT NULL AND trim(CAST(name AS VARCHAR)) <> ''
                          AND longitude IS NOT NULL AND latitude IS NOT NULL
                          AND {tile_pred}
                          AND {scope_pred}
                    ), norm AS (
                        SELECT *, {name_norm} AS name_norm
                        FROM raw
                    )
                    SELECT source, source_id, name, category, lon, lat, provenance, upstream_license,
                           name_norm,
                           array_to_string(
                               list_sort(string_split(name_norm, ' ')), ' '
                           ) AS name_tokens
                    FROM norm
                    WHERE name_norm <> ''
                    ORDER BY lon, lat
                ) TO '{tmp.as_posix()}' (
                    FORMAT PARQUET,
                    COMPRESSION ZSTD,
                    ROW_GROUP_SIZE 25000
                )
                """
            )
            con.close()
            con = None

            # Do not promote a truncated/invalid file after a remote error.
            if not valid_parquet(tmp):
                raise RuntimeError("Foursquare query produced an invalid Parquet tile.")

            os.replace(tmp, target)
            return

        except Exception as exc:
            last_error = exc
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            tmp.unlink(missing_ok=True)

            if attempt < retries:
                delay = min(30, 5 * attempt)
                print(
                    f"[Foursquare] {tile.key} attempt {attempt}/{retries} failed: "
                    f"{exc}. Retrying in {delay}s...",
                    flush=True,
                )
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def prepare_foursquare(
    root: Path,
    scope: Scope,
    source_tiles: list[Tile],
    workers: int,
    temp_dir: Path,
    memory_limit: str,
    *,
    refresh: bool = False,
) -> Path:
    """Normalize every missing Foursquare block of the pinned release.

    ``refresh`` re-pins the newest release. Existing blocks are kept when the
    newest release is the one already pinned; otherwise the directory is
    discarded before the first new block is written (see prepare_overture).
    """
    out_dir = root / "data" / "sources" / scope.slug / "fsq"
    # Record <= 0.2.0 tiles under the release pinned before this run.
    adopt_unbound_dir(out_dir, pinned_release(root, "fsq"), "Foursquare")
    release = resolve_fsq_release(root, refresh=refresh)
    print(f"[Foursquare] pinned release {release}")

    bind_dir_to_release(out_dir, release, "Foursquare")
    todo = [t for t in source_tiles if not valid_parquet(out_dir / f"{t.key}.parquet")]
    print(f"[Foursquare] {len(source_tiles) - len(todo)}/{len(source_tiles)} blocks cached")

    _run_tiles(
        _fsq_worker, todo, workers,
        str(root), scope.bboxes, scope.slug, release, str(temp_dir), memory_limit,
    )
    return out_dir


@lru_cache(maxsize=4096)
def _regular_source_tile_files_cached(source_dir: Path, key: str) -> tuple[Path, ...]:
    path = source_dir / f"{key}.parquet"
    return (path,) if valid_parquet(path) else ()


def regular_source_tile_files(source_dir: Path, tile: Tile) -> list[Path]:
    """Resolve the FSQ/Overture file for one source tile.

    Cached for the same reason as :func:`osm_tile_files`: ``valid_parquet``
    opens the Parquet footer, and matching asked the same question tens of
    thousands of times per national run. Source tiles are immutable once the
    acquisition stage has finished, which is the only time this is called.
    """
    return list(_regular_source_tile_files_cached(source_dir, tile.key))


def clear_tile_file_caches() -> None:
    """Forget cached tile listings. Each stage calls this once at its start,
    so a stage never uses a listing made before acquisition changed a file."""
    _osm_tile_files_cached.cache_clear()
    _regular_source_tile_files_cached.cache_clear()


# ---------------------------------------------------------------------------
# Philippine land tile mask
# ---------------------------------------------------------------------------

def filter_tiles_to_boundary(
    tiles: list[Tile],
    boundary: Path,
    temp_dir: Path,
    memory_limit: str,
) -> list[Tile]:
    """Drop 1-degree ocean-only blocks before FSQ/Overture acquisition."""
    if not valid_parquet(boundary) or not tiles:
        return tiles

    values = ",".join(
        f"({t.ix},{t.iy},{t.west},{t.south},{t.east},{t.north})" for t in tiles
    )
    con = connect(temp_dir / "tile_mask", memory_limit=memory_limit, threads=1, spatial=True)
    rows = con.execute(
        f"""
        WITH t(ix,iy,w,s,e,n) AS (VALUES {values})
        SELECT DISTINCT t.ix, t.iy
        FROM t, read_parquet('{boundary.as_posix()}') b
        WHERE ST_Intersects(
                  ST_SetCRS(ST_MakeEnvelope(w,s,e,n), '{CRS84}'),
                  ST_SetCRS(b.geometry::GEOMETRY, '{CRS84}')
              )
        """
    ).fetchall()
    con.close()

    keep = {(int(ix), int(iy)) for ix, iy in rows}
    return [t for t in tiles if (t.ix, t.iy) in keep]


def source_tiles_for_scope(scope: Scope, source_tile_deg: float) -> list[Tile]:
    return tiles_for_bboxes(scope.bboxes, source_tile_deg)
