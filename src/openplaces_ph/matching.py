"""Low-memory cross-source entity matching.

The previous implementation fuzzy-scored candidate pairs one Python row at a
time. That is accurate but unnecessarily expensive on a 2016/2017-era CPU.
This edition does the whole candidate generation + string scoring in DuckDB:

* numeric grid blocking prevents an all-pairs join;
* a separable degree-bound prefilter runs *before* any trigonometry, so exact
  Haversine is only evaluated for pairs that could plausibly be within range;
* only POIs within ``max_distance_m`` survive;
* names were normalized once at source ingestion;
* DuckDB's native Jaro-Winkler similarity scores both original token order and
  alphabetically sorted tokens, which handles e.g. "SM North Starbucks" versus
  "Starbucks SM North" without Python/RapidFuzz overhead;
* every 0.25-degree tile x source-pair is an atomic Parquet checkpoint;
* the checkpoint manifest records the source snapshot, so shards built from
  another source vintage are never reused, and ``_COMPLETE.json`` tells
  finalization that every shard of the current snapshot exists.

Single source of truth for thresholds
-------------------------------------
The distance/name acceptance ladder is defined **once** in
:func:`acceptance_bands`.  Both the SQL executed by DuckDB and the pure-Python
:func:`accept_pair` are generated from it, so the unit-tested Python mirror
cannot silently drift from the SQL that actually produces the data.
"""

from __future__ import annotations

import hashlib
import math
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Scope, bbox_sql
from .db import connect
from .source_set import ALL_PAIRS, ALL_SOURCES, normalize_sources, pair_dir_name, source_pairs
from .snapshot import PipelineStateError, discard_dir, source_snapshot
from .sources import (
    clear_tile_file_caches,
    osm_tile_files,
    overture_fsq_provenance_sql,
    regular_source_tile_files,
)
from .tiles import Tile, bbox_with_halo, child_tiles, intersects, tiles_for_bboxes
from .util import atomic_json, quote_paths, read_json, valid_parquet

EARTH_RADIUS_M = 6_371_008.8
PAIRS = ALL_PAIRS

MANIFEST_NAME = "_config.json"
COMPLETE_NAME = "_COMPLETE.json"

# Geodesy constants used to convert a metre budget into a *safe upper bound* in
# degrees anywhere in the Philippines.
#
# * 1 degree of latitude is never shorter than ~110,574 m (it grows towards the
#   poles), so dividing by the minimum yields the largest possible degree span.
# * 1 degree of longitude is 111,320 m x cos(latitude).  The Philippine bbox
#   reaches 21.30 N, so cos(21.5 deg) is a conservative worst case.
METRES_PER_DEG_LAT_MIN = 110_574.0
METRES_PER_DEG_LON_EQUATOR = 111_320.0
MIN_COS_LAT_PH = math.cos(math.radians(21.5))
METRES_PER_DEG_LON_MIN_PH = METRES_PER_DEG_LON_EQUATOR * MIN_COS_LAT_PH

# Short/generic business labels are dangerous in malls, campuses, airports,
# markets, etc. They require essentially exact names at very short distance.
GENERIC_NAMES = {
    "atm", "office", "store", "shop", "canteen", "market", "restaurant",
    "school", "church", "clinic", "hospital", "pharmacy", "bank", "terminal",
}

GENERIC_MAX_DISTANCE_M = 15.0
GENERIC_MIN_NAME_SCORE = 0.99
GENERIC_MAX_NAME_LENGTH = 4

# Jaro-Winkler score_cutoff. Anything below the *lowest* band threshold cannot
# be accepted, so telling DuckDB to short-circuit there is free accuracy-wise.
JW_SCORE_CUTOFF = 0.65

EDGE_SCHEMA = pa.schema([
    ("left_source", pa.string()),
    ("left_source_id", pa.string()),
    ("right_source", pa.string()),
    ("right_source_id", pa.string()),
    ("distance_m", pa.float64()),
    ("name_score", pa.float32()),
    ("category_score", pa.float32()),
    ("score", pa.float32()),
    ("independent", pa.bool_()),
])


@dataclass(frozen=True)
class MatchConfig:
    # Wide enough to accommodate coordinate placement differences between
    # providers, but strict name thresholds rise rapidly with distance.
    max_distance_m: float = 120.0

    # Multiplicative headroom applied to every metre->degree conversion so
    # floating-point edges never cost us a genuine candidate pair.
    safety: float = 1.02

    def __post_init__(self) -> None:
        """Reject settings that could make the blocking guarantee meaningless.

        The CLI already checks ``--max-distance``, but this dataclass is also
        imported directly by tests and by people who may build on the library.
        Rejecting NaN/infinity here prevents surprising SQL such as a negative
        grid size or an unbounded distance literal.  ``safety`` must be at least
        1.0 because values below one could shrink the grid enough to lose genuine
        in-range pairs before Haversine ever sees them.
        """
        if not math.isfinite(self.max_distance_m) or self.max_distance_m <= 0:
            raise ValueError("max_distance_m must be a positive finite number")
        if not math.isfinite(self.safety) or self.safety < 1.0:
            raise ValueError("safety must be finite and at least 1.0")

    @property
    def grid_degrees(self) -> float:
        """Blocking cell size, derived from ``max_distance_m``.

        A 3x3 neighbour join only guarantees coverage out to *one* cell width.
        Deriving the cell from the search radius (instead of hard-coding it)
        keeps ``--max-distance`` correct at any value, and the tighter default
        cell emits substantially fewer candidate pairs than a fixed 0.0015 deg.
        """
        return (self.max_distance_m * self.safety) / METRES_PER_DEG_LON_MIN_PH

    @property
    def delta_lat_degrees(self) -> float:
        """Upper bound on the latitude separation of an in-range pair."""
        return (self.max_distance_m * self.safety) / METRES_PER_DEG_LAT_MIN

    @property
    def delta_lon_degrees(self) -> float:
        """Upper bound on the longitude separation of an in-range pair."""
        return (self.max_distance_m * self.safety) / METRES_PER_DEG_LON_MIN_PH

    def describe(self) -> dict:
        """Config fingerprint recorded next to the edge checkpoints."""
        return {
            "max_distance_m": self.max_distance_m,
            "safety": self.safety,
            "grid_degrees": self.grid_degrees,
            "bands": [list(b) for b in acceptance_bands(self.max_distance_m)],
            "generic_max_distance_m": GENERIC_MAX_DISTANCE_M,
            "generic_min_name_score": GENERIC_MIN_NAME_SCORE,
            "scorer": "duckdb_jaro_winkler_name_and_sorted_tokens_v1",
        }


# ---------------------------------------------------------------------------
# Acceptance ladder: one definition, two renderings
# ---------------------------------------------------------------------------

# The matching calibration is a *piecewise* rule.  The score attached to a
# distance interval belongs to that interval even when the user chooses a
# smaller maximum radius.  For example, a 60 m run should still use 0.90 for
# the 50-60 m slice; it must NOT suddenly inherit the 0.94 rule that normally
# applies only beyond 90 m.
#
# The final ``math.inf`` band is not written to SQL as infinity.  It simply
# tells ``acceptance_bands`` which threshold applies to the tail of the
# configured radius.
CALIBRATED_BANDS: tuple[tuple[float, float], ...] = (
    (20.0, 0.70),
    (50.0, 0.82),
    (90.0, 0.90),
    (math.inf, 0.94),
)


def acceptance_bands(max_distance_m: float) -> tuple[tuple[float, float], ...]:
    """Return the calibrated distance/name ladder truncated at ``max_distance_m``.

    Think of the calibration as four distance intervals::

        0-20 m    -> name score >= 0.70
        20-50 m   -> name score >= 0.82
        50-90 m   -> name score >= 0.90
        >90 m     -> name score >= 0.94

    ``--max-distance`` changes where we stop looking; it does **not** change the
    threshold inside the final partial interval.  That distinction matters for
    non-default radii such as 30 m or 60 m.
    """
    if not math.isfinite(max_distance_m) or max_distance_m <= 0:
        raise ValueError("max_distance_m must be a positive finite number")

    result: list[tuple[float, float]] = []
    max_d = float(max_distance_m)
    for upper, min_score in CALIBRATED_BANDS:
        if max_d <= upper:
            result.append((max_d, min_score))
            break
        result.append((float(upper), min_score))
    return tuple(result)


def generic_expr(column: str) -> str:
    """SQL predicate: is this normalized name too generic to merge loosely?"""
    values = ",".join("'" + x.replace("'", "''") + "'" for x in sorted(GENERIC_NAMES))
    return f"({column} IN ({values}) OR length({column}) <= {GENERIC_MAX_NAME_LENGTH})"


def acceptance_sql(
    cfg: MatchConfig,
    distance_col: str = "distance_m",
    score_col: str = "name_score",
    left_name_col: str = "name_left",
    right_name_col: str = "name_right",
) -> str:
    """Render the acceptance ladder as one boolean SQL expression.

    ``tests/test_threshold_parity.py`` evaluates this expression in DuckDB and
    asserts it agrees with :func:`accept_pair` on random inputs.
    """
    # Generic labels get an even stricter rule, but the global radius remains
    # absolute.  The explicit ``least`` also keeps this helper correct when it
    # is tested outside the candidate CTE (where the upstream distance filter is
    # not present).
    generic_limit = min(GENERIC_MAX_DISTANCE_M, cfg.max_distance_m)
    ladder = ["CASE"]
    ladder.append(
        f" WHEN {generic_expr(left_name_col)} OR {generic_expr(right_name_col)} "
        f"THEN {distance_col} <= {generic_limit} "
        f"AND {score_col} >= {GENERIC_MIN_NAME_SCORE}"
    )
    for upper, min_score in acceptance_bands(cfg.max_distance_m):
        ladder.append(f" WHEN {distance_col} <= {upper} THEN {score_col} >= {min_score}")
    ladder.append(" ELSE false END")
    return (
        f"(length(coalesce({left_name_col}, '')) > 0 "
        f"AND length(coalesce({right_name_col}, '')) > 0 "
        f"AND {''.join(ladder)})"
    )


def accept_pair(
    distance_m: float,
    name_score: float,
    left_norm: str,
    right_norm: str,
    max_distance_m: float = 120.0,
) -> bool:
    """Pure-Python mirror of :func:`acceptance_sql`.

    Kept in lockstep with the SQL by a parity test rather than by hand.
    """
    if not left_norm or not right_norm:
        return False
    generic = (
        left_norm in GENERIC_NAMES
        or right_norm in GENERIC_NAMES
        or len(left_norm) <= GENERIC_MAX_NAME_LENGTH
        or len(right_norm) <= GENERIC_MAX_NAME_LENGTH
    )
    if generic:
        return (
            distance_m <= min(GENERIC_MAX_DISTANCE_M, max_distance_m)
            and name_score >= GENERIC_MIN_NAME_SCORE
        )
    for upper, min_score in acceptance_bands(max_distance_m):
        if distance_m <= upper:
            return name_score >= min_score
    return False


# ---------------------------------------------------------------------------
# Candidate generation
# ---------------------------------------------------------------------------

def _candidate_sql(
    a_files: list[Path],
    b_files: list[Path],
    a: str,
    b: str,
    core: Tile,
    scope: Scope,
    cfg: MatchConfig,
) -> str:
    """Return one SQL query that produces only *accepted* entity links."""
    halo = bbox_with_halo(core.bbox, cfg.max_distance_m)
    aw, as_, ae, an = core.bbox
    bw, bs, be, bn = halo
    scope_pred = bbox_sql("lon", "lat", scope.bboxes)
    g = cfg.grid_degrees
    max_d = cfg.max_distance_m
    dlat = cfg.delta_lat_degrees
    dlon = cfg.delta_lon_degrees

    if {a, b} == {"fsq", "overture"}:
        ov_provenance = "right_provenance" if b == "overture" else "left_provenance"
        independent_expr = f"NOT {overture_fsq_provenance_sql(ov_provenance)}"
    else:
        independent_expr = "true"

    # The query is intentionally staged. DuckDB can push the bbox predicates
    # into Parquet row groups before the more expensive similarity functions,
    # and the cheap separable degree bounds in the join keep trigonometry off
    # grid-adjacent pairs that are obviously out of range.
    return f"""
    WITH aa AS (
        SELECT source_id, name, category, provenance, name_norm, name_tokens, lon, lat,
               CAST(floor(lon / {g}) AS BIGINT) AS gx,
               CAST(floor(lat / {g}) AS BIGINT) AS gy
        FROM read_parquet({quote_paths(a_files)}, union_by_name=true)
        WHERE lon >= {aw} AND lon < {ae} AND lat >= {as_} AND lat < {an}
          AND {scope_pred}
    ),
    bb AS (
        SELECT source_id, name, category, provenance, name_norm, name_tokens, lon, lat,
               CAST(floor(lon / {g}) AS BIGINT) AS gx,
               CAST(floor(lat / {g}) AS BIGINT) AS gy
        FROM read_parquet({quote_paths(b_files)}, union_by_name=true)
        WHERE lon >= {bw} AND lon < {be} AND lat >= {bs} AND lat < {bn}
          AND {scope_pred}
    ),
    aneigh AS (
        SELECT aa.*, aa.gx + dx AS ngx, aa.gy + dy AS ngy
        FROM aa
        CROSS JOIN UNNEST([-1, 0, 1]) AS x(dx)
        CROSS JOIN UNNEST([-1, 0, 1]) AS y(dy)
    ),
    boxed AS (
        SELECT
            a.source_id AS left_source_id,
            b.source_id AS right_source_id,
            a.name_norm AS name_left,
            b.name_norm AS name_right,
            a.name_tokens AS tokens_left,
            b.name_tokens AS tokens_right,
            a.category AS left_category,
            b.category AS right_category,
            a.provenance AS left_provenance,
            b.provenance AS right_provenance,
            a.lon AS left_lon, a.lat AS left_lat,
            b.lon AS right_lon, b.lat AS right_lat
        FROM aneigh a
        JOIN bb b
          ON a.ngx = b.gx AND a.ngy = b.gy
         AND b.lat BETWEEN a.lat - {dlat} AND a.lat + {dlat}
         AND b.lon BETWEEN a.lon - {dlon} AND a.lon + {dlon}
    ),
    near AS (
        SELECT * EXCLUDE (left_lon, left_lat, right_lon, right_lat),
            2 * {EARTH_RADIUS_M} * asin(sqrt(
                pow(sin(radians(right_lat - left_lat) / 2), 2)
                + cos(radians(left_lat)) * cos(radians(right_lat))
                * pow(sin(radians(right_lon - left_lon) / 2), 2)
            )) AS distance_m
        FROM boxed
    ),
    within_distance AS (
        SELECT *
        FROM near
        WHERE distance_m <= {max_d}
    ),
    named AS (
        SELECT *,
            greatest(
                jaro_winkler_similarity(name_left, name_right, {JW_SCORE_CUTOFF}),
                jaro_winkler_similarity(tokens_left, tokens_right, {JW_SCORE_CUTOFF})
            ) AS name_score
        FROM within_distance
    ),
    accepted AS (
        SELECT *
        FROM named
        WHERE {acceptance_sql(cfg)}
    ),
    scored AS (
        SELECT *,
            CASE
                WHEN coalesce(left_category,'') = '' OR coalesce(right_category,'') = '' THEN 0.0
                ELSE jaro_winkler_similarity(lower(left_category), lower(right_category), 0.0)
            END AS category_score
        FROM accepted
    )
    SELECT
        '{a}'::VARCHAR AS left_source,
        CAST(left_source_id AS VARCHAR) AS left_source_id,
        '{b}'::VARCHAR AS right_source,
        CAST(right_source_id AS VARCHAR) AS right_source_id,
        distance_m::DOUBLE AS distance_m,
        name_score::FLOAT AS name_score,
        category_score::FLOAT AS category_score,
        (
            0.80 * name_score
            + 0.18 * (1.0 - least(distance_m, {max_d}) / {max_d})
            + 0.02 * category_score
        )::FLOAT AS score,
        ({independent_expr})::BOOLEAN AS independent
    FROM scored
    """


def _write_empty_edge(path: Path) -> None:
    tmp = path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    pq.write_table(
        pa.Table.from_arrays([pa.array([], type=f.type) for f in EDGE_SCHEMA], schema=EDGE_SCHEMA),
        tmp,
        compression="zstd",
    )
    os.replace(tmp, path)


def _files_for_bbox(
    source: str,
    bbox: tuple[float, float, float, float],
    source_tile_deg: float,
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
    allowed: frozenset[tuple[int, int]] | None = None,
) -> list[Path]:
    """Return only the 1-degree source files touching a small bbox/halo.

    ``allowed`` holds the (ix, iy) indices of the run's source tiles. OSM is
    partitioned for the whole country, so without this filter a halo could
    read OSM observations from a block that the land mask removed. Finalization
    never reads such a block, so one accepted edge into it made the national
    edge-resolution check fail at the very end of a run.
    """
    files: list[Path] = []
    for tile in tiles_for_bboxes([bbox], source_tile_deg):
        if allowed is not None and (tile.ix, tile.iy) not in allowed:
            continue
        if source == "fsq":
            files.extend(regular_source_tile_files(fsq_dir, tile))
        elif source == "overture":
            files.extend(regular_source_tile_files(overture_dir, tile))
        elif source == "osm":
            files.extend(osm_tile_files(osm_dir, tile))
        else:
            raise ValueError(source)
    return sorted(set(files))


def _match_block_worker(
    block: Tile,
    root_s: str,
    scope: Scope,
    source_tile_deg: float,
    match_tile_deg: float,
    fsq_s: str,
    overture_s: str,
    osm_s: str,
    temp_s: str,
    memory_limit: str,
    cfg: MatchConfig,
    stop_s: str,
    allowed: frozenset[tuple[int, int]] | None = None,
    pairs: tuple[tuple[str, str], ...] = PAIRS,
) -> None:
    """Process one 1-degree parent block and checkpoint every child/pair."""
    root = Path(root_s)
    fsq_dir, overture_dir, osm_dir = Path(fsq_s), Path(overture_s), Path(osm_s)
    edge_root = root / "data" / "work" / scope.slug / "edges"
    stop_file = Path(stop_s)

    if not pairs:
        return

    con = connect(Path(temp_s) / f"match_{os.getpid()}", memory_limit=memory_limit, threads=1)
    try:
        for core in child_tiles(block, match_tile_deg):
            if stop_file.exists():
                return
            if not any(intersects(core.bbox, b) for b in scope.bboxes):
                continue

            for a, b in pairs:
                if stop_file.exists():
                    return

                out_dir = edge_root / pair_dir_name(a, b)
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / f"{core.key}.parquet"
                if valid_parquet(target):
                    continue
                target.unlink(missing_ok=True)

                a_files = _files_for_bbox(
                    a, core.bbox, source_tile_deg, fsq_dir, overture_dir, osm_dir, allowed
                )
                if not a_files:
                    # FSQ/Overture record even an empty block as a real file,
                    # so a missing file means "not downloaded". An empty shard
                    # written now would be skipped by every later run.
                    raise PipelineStateError(
                        f"{a} source tile for block {block.key} is missing; "
                        "refusing to write an empty match shard. Run --only sources first."
                    )
                b_files = _files_for_bbox(
                    b, bbox_with_halo(core.bbox, cfg.max_distance_m),
                    source_tile_deg, fsq_dir, overture_dir, osm_dir, allowed,
                )
                if not b_files:
                    # Legitimately empty, e.g. no OSM partition exists because
                    # the block has no named OSM POIs.
                    _write_empty_edge(target)
                    continue

                tmp = target.with_suffix(".parquet.part")
                tmp.unlink(missing_ok=True)
                sql = _candidate_sql(a_files, b_files, a, b, core, scope, cfg)

                # Everything expensive remains inside DuckDB. No national or
                # tile-sized candidate table is materialized in Python memory.
                con.execute(
                    f"""
                    COPY ({sql}) TO '{tmp.as_posix()}' (
                        FORMAT PARQUET,
                        COMPRESSION ZSTD,
                        ROW_GROUP_SIZE 50000
                    )
                    """
                )
                os.replace(tmp, target)
    finally:
        con.close()


def _expected_shards(
    edge_root: Path,
    scope: Scope,
    source_tiles: list[Tile],
    match_tile_deg: float,
    pairs: tuple[tuple[str, str], ...] = PAIRS,
) -> list[Path]:
    shards: list[Path] = []
    for block in source_tiles:
        for core in child_tiles(block, match_tile_deg):
            if any(intersects(core.bbox, b) for b in scope.bboxes):
                shards.extend(
                    edge_root / pair_dir_name(a, b) / f"{core.key}.parquet"
                    for a, b in pairs
                )
    return shards


def _inventory_payload(names: list[str]) -> dict[str, object]:
    """Compact, stable identity of an exact set of shard paths."""
    digest = hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest()
    return {"count": len(names), "paths_sha256": digest}


def _shard_inventory(edge_root: Path) -> tuple[dict[str, object], list[Path]]:
    """Return the current Parquet shard inventory under ``edge_root``."""
    paths = sorted(
        edge_root.rglob("*.parquet"),
        key=lambda p: p.relative_to(edge_root).as_posix(),
    )
    names = [p.relative_to(edge_root).as_posix() for p in paths]
    return _inventory_payload(names), paths


def match_checkpoints_complete(
    edge_root: Path,
    *,
    validate_parquet: bool = False,
) -> bool:
    """True only when the completion marker still describes the shard set.

    ``validate_parquet`` is deliberately optional: ``--status`` needs a cheap
    integrity check, while finalization pays the one-time cost of opening every
    shard footer before it trusts those files as input.
    """
    manifest = read_json(edge_root / MANIFEST_NAME)
    complete = read_json(edge_root / COMPLETE_NAME)
    if manifest is None or complete is None or complete.get("manifest") != manifest:
        return False
    recorded = complete.get("shards")
    if not isinstance(recorded, dict):
        return False
    actual, paths = _shard_inventory(edge_root)
    if recorded != actual:
        return False
    return not validate_parquet or all(valid_parquet(path) for path in paths)


def prepare_matches(
    root: Path,
    scope: Scope,
    source_tiles: list[Tile],
    source_tile_deg: float,
    match_tile_deg: float,
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
    workers: int,
    temp_dir: Path,
    memory_limit: str,
    cfg: MatchConfig,
    sources: tuple[str, ...] = ALL_SOURCES,
    *,
    rebuild: bool = False,
) -> Path:
    """Create all pairwise edge shards; safe to interrupt and rerun.

    Refuses to start unless every source layer is complete and belongs to one
    snapshot. The manifest stores that snapshot, so shards are rebuilt
    automatically after any source refresh, however the stages are split
    across sessions.
    """
    clear_tile_file_caches()
    sources = normalize_sources(sources, default=ALL_SOURCES)
    pairs = source_pairs(sources)
    snapshot = source_snapshot(
        root, source_tiles, fsq_dir, overture_dir, osm_dir, sources
    )

    edge_root = root / "data" / "work" / scope.slug / "edges"
    manifest = edge_root / MANIFEST_NAME
    config_payload = dict(
        cfg.describe(),
        source_tile_deg=source_tile_deg,
        match_tile_deg=match_tile_deg,
        sources=snapshot,
    )
    config_payload["active_sources"] = list(sources)
    config_payload["source_tiles"] = [tile.key for tile in source_tiles]

    if not rebuild:
        previous = read_json(manifest)
        if previous is None:
            if edge_root.exists() and any(edge_root.rglob("*.parquet")):
                print("[match] edge checkpoints have no readable manifest; rebuilding them")
                rebuild = True
        elif previous != config_payload:
            if "sources" not in previous:
                print(
                    "[match] edge checkpoints were written by <= 0.2.0 without a record of "
                    "their source snapshot; rebuilding them once"
                )
            else:
                print("[match] matching configuration or source snapshot changed; rebuilding edge checkpoints")
            rebuild = True

    if rebuild:
        discard_dir(edge_root)
    edge_root.mkdir(parents=True, exist_ok=True)
    (edge_root / COMPLETE_NAME).unlink(missing_ok=True)
    atomic_json(manifest, config_payload)

    allowed = frozenset((t.ix, t.iy) for t in source_tiles)
    workers = max(1, min(workers, len(source_tiles) or 1))
    stop_file = edge_root / ".stop"
    stop_file.unlink(missing_ok=True)

    # For the low-resource profile this is intentionally 1 worker. The code still
    # supports 2 if the user later confirms the machine has SSD + spare RAM.
    if workers == 1:
        try:
            for block in source_tiles:
                _match_block_worker(
                    block, str(root), scope, source_tile_deg, match_tile_deg,
                    str(fsq_dir), str(overture_dir), str(osm_dir), str(temp_dir),
                    memory_limit, cfg, str(stop_file), allowed, pairs,
                )
        except KeyboardInterrupt:
            stop_file.touch()
            raise
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(
                    _match_block_worker, block, str(root), scope,
                    source_tile_deg, match_tile_deg,
                    str(fsq_dir), str(overture_dir), str(osm_dir), str(temp_dir),
                    memory_limit, cfg, str(stop_file), allowed, pairs,
                )
                for block in source_tiles
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except BaseException:
                # Ctrl+C *or* a failed block: ask the other workers to stop at
                # their next shard instead of finishing whole blocks first.
                stop_file.touch()
                for future in futures:
                    future.cancel()
                raise

    stop_file.unlink(missing_ok=True)

    # Seal the exact shard inventory. Merely checking _COMPLETE.json later is
    # insufficient: a shard can be deleted/corrupted after the marker is
    # written, and an unexpected stale .parquet would be consumed by finalize.
    expected = sorted(
        set(_expected_shards(
            edge_root, scope, source_tiles, match_tile_deg, pairs
        )),
        key=lambda p: p.relative_to(edge_root).as_posix(),
    )
    bad = [p for p in expected if not valid_parquet(p)]
    if bad:
        raise PipelineStateError(
            f"Matching finished with {len(bad)} missing or invalid shard(s) "
            f"(first: {bad[0]}). "
            "Rerun --only match."
        )
    expected_names = [p.relative_to(edge_root).as_posix() for p in expected]
    expected_inventory = _inventory_payload(expected_names)
    actual_inventory, _ = _shard_inventory(edge_root)
    if actual_inventory != expected_inventory:
        raise PipelineStateError(
            "Matching finished with an unexpected edge-shard inventory. "
            "Rerun --only match with --rebuild-match."
        )
    atomic_json(
        edge_root / COMPLETE_NAME,
        {"manifest": config_payload, "shards": expected_inventory},
    )
    return edge_root
