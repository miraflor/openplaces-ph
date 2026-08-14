"""Low-memory cross-source entity matching.

The previous implementation fuzzy-scored candidate pairs one Python row at a
time. That is accurate but unnecessarily expensive on a 2016/2017-era CPU.
This edition does the whole candidate generation + string scoring in DuckDB:

* numeric grid blocking prevents an all-pairs join;
* only POIs within 120 m survive;
* names were normalized once at source ingestion;
* DuckDB's native Jaro-Winkler similarity scores both original token order and
  alphabetically sorted tokens, which handles e.g. "SM North Starbucks" versus
  "Starbucks SM North" without Python/RapidFuzz overhead;
* every 0.25-degree tile × source-pair is an atomic Parquet checkpoint.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .config import Scope, bbox_sql
from .db import connect
from .sources import osm_tile_files, regular_source_tile_files
from .tiles import Tile, bbox_with_halo, child_tiles, intersects, tiles_for_bboxes
from .util import atomic_json, quote_paths, valid_parquet

EARTH_RADIUS_M = 6_371_008.8
PAIRS = (("fsq", "overture"), ("fsq", "osm"), ("overture", "osm"))

# Short/generic business labels are dangerous in malls, campuses, airports,
# markets, etc. They require essentially exact names at very short distance.
GENERIC_NAMES = {
    "atm", "office", "store", "shop", "canteen", "market", "restaurant",
    "school", "church", "clinic", "hospital", "pharmacy", "bank", "terminal",
}

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

    # 0.0015 degree is ~155-167 m across Philippine latitudes. Therefore a
    # 3×3 neighbor-cell join safely covers the 120 m search radius.
    grid_degrees: float = 0.0015


def accept_pair(distance_m: float, name_score: float, left_norm: str, right_norm: str) -> bool:
    """Pure-Python mirror of the SQL thresholds, mainly for unit tests."""
    if not left_norm or not right_norm:
        return False
    generic = (
        left_norm in GENERIC_NAMES
        or right_norm in GENERIC_NAMES
        or len(left_norm) <= 4
        or len(right_norm) <= 4
    )
    if generic:
        return distance_m <= 15 and name_score >= 0.99
    if distance_m <= 20:
        return name_score >= 0.70
    if distance_m <= 50:
        return name_score >= 0.82
    if distance_m <= 90:
        return name_score >= 0.90
    return distance_m <= 120 and name_score >= 0.94


def _generic_sql(alias: str) -> str:
    values = ",".join("'" + x.replace("'", "''") + "'" for x in sorted(GENERIC_NAMES))
    return f"({alias}.name_norm IN ({values}) OR length({alias}.name_norm) <= 4)"


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
    scope_a = bbox_sql("lon", "lat", scope.bboxes)
    scope_b = bbox_sql("lon", "lat", scope.bboxes)
    g = cfg.grid_degrees
    max_d = cfg.max_distance_m

    generic_a = _generic_sql("s")
    generic_b = _generic_sql("s")  # rewritten below for the right-name aliases
    # _generic_sql emits "s.name_norm"; simple replacement keeps the generic
    # vocabulary defined in exactly one place.
    generic_left = generic_a.replace("s.name_norm", "name_left")
    generic_right = generic_b.replace("s.name_norm", "name_right")

    if {a, b} == {"fsq", "overture"}:
        ov_provenance = "right_provenance" if b == "overture" else "left_provenance"
        independent_expr = f"NOT regexp_matches(lower(coalesce({ov_provenance},'')), 'foursquare|\\bfsq\\b')"
    else:
        independent_expr = "true"

    # The query is intentionally staged. DuckDB can push the bbox predicates
    # into Parquet row groups before the more expensive similarity functions.
    return f"""
    WITH aa AS (
        SELECT source_id, name, category, provenance, name_norm, name_tokens, lon, lat,
               CAST(floor(lon / {g}) AS BIGINT) AS gx,
               CAST(floor(lat / {g}) AS BIGINT) AS gy
        FROM read_parquet({quote_paths(a_files)}, union_by_name=true)
        WHERE lon >= {aw} AND lon < {ae} AND lat >= {as_} AND lat < {an}
          AND {scope_a}
    ),
    bb AS (
        SELECT source_id, name, category, provenance, name_norm, name_tokens, lon, lat,
               CAST(floor(lon / {g}) AS BIGINT) AS gx,
               CAST(floor(lat / {g}) AS BIGINT) AS gy
        FROM read_parquet({quote_paths(b_files)}, union_by_name=true)
        WHERE lon >= {bw} AND lon < {be} AND lat >= {bs} AND lat < {bn}
          AND {scope_b}
    ),
    aneigh AS (
        SELECT aa.*, aa.gx + dx AS ngx, aa.gy + dy AS ngy
        FROM aa
        CROSS JOIN UNNEST([-1, 0, 1]) AS x(dx)
        CROSS JOIN UNNEST([-1, 0, 1]) AS y(dy)
    ),
    near AS (
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
            2 * {EARTH_RADIUS_M} * asin(sqrt(
                pow(sin(radians(b.lat - a.lat) / 2), 2)
                + cos(radians(a.lat)) * cos(radians(b.lat))
                * pow(sin(radians(b.lon - a.lon) / 2), 2)
            )) AS distance_m
        FROM aneigh a
        JOIN bb b ON a.ngx = b.gx AND a.ngy = b.gy
    ),
    within_distance AS (
        SELECT *
        FROM near
        WHERE distance_m <= {max_d}
    ),
    named AS (
        SELECT *,
            greatest(
                jaro_winkler_similarity(name_left, name_right, 0.65),
                jaro_winkler_similarity(tokens_left, tokens_right, 0.65)
            ) AS name_score
        FROM within_distance
    ),
    accepted AS (
        SELECT *
        FROM named
        WHERE
            CASE
                WHEN {generic_left} OR {generic_right}
                    THEN distance_m <= 15 AND name_score >= 0.99
                WHEN distance_m <= 20 THEN name_score >= 0.70
                WHEN distance_m <= 50 THEN name_score >= 0.82
                WHEN distance_m <= 90 THEN name_score >= 0.90
                ELSE name_score >= 0.94
            END
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
) -> list[Path]:
    """Return only the 1-degree source files touching a small bbox/halo."""
    files: list[Path] = []
    for tile in tiles_for_bboxes([bbox], source_tile_deg):
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
) -> None:
    """Process one 1-degree parent block and checkpoint every child/pair."""
    root = Path(root_s)
    fsq_dir, overture_dir, osm_dir = Path(fsq_s), Path(overture_s), Path(osm_s)
    edge_root = root / "data" / "work" / scope.slug / "edges"
    stop_file = Path(stop_s)

    con = connect(Path(temp_s) / f"match_{os.getpid()}", memory_limit=memory_limit, threads=1)
    try:
        for core in child_tiles(block, match_tile_deg):
            if stop_file.exists():
                return
            if not any(intersects(core.bbox, b) for b in scope.bboxes):
                continue

            for a, b in PAIRS:
                if stop_file.exists():
                    return

                out_dir = edge_root / f"{a}__{b}"
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / f"{core.key}.parquet"
                if valid_parquet(target):
                    continue
                target.unlink(missing_ok=True)

                a_files = _files_for_bbox(
                    a, core.bbox, source_tile_deg, fsq_dir, overture_dir, osm_dir
                )
                b_files = _files_for_bbox(
                    b, bbox_with_halo(core.bbox, cfg.max_distance_m),
                    source_tile_deg, fsq_dir, overture_dir, osm_dir,
                )

                if not a_files or not b_files:
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
    *,
    rebuild: bool = False,
) -> Path:
    """Create all pairwise edge shards; safe to interrupt and rerun."""
    edge_root = root / "data" / "work" / scope.slug / "edges"
    manifest = edge_root / "_config.json"
    config_payload = {
        "source_tile_deg": source_tile_deg,
        "match_tile_deg": match_tile_deg,
        "max_distance_m": cfg.max_distance_m,
        "grid_degrees": cfg.grid_degrees,
        "scorer": "duckdb_jaro_winkler_name_and_sorted_tokens_v1",
    }

    if manifest.exists() and not rebuild:
        import json
        try:
            previous = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            previous = None
        if previous != config_payload:
            print("[match] matching configuration changed; rebuilding edge checkpoints")
            rebuild = True

    if rebuild and edge_root.exists():
        shutil.rmtree(edge_root)
    edge_root.mkdir(parents=True, exist_ok=True)
    atomic_json(manifest, config_payload)

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
                    memory_limit, cfg, str(stop_file),
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
                    memory_limit, cfg, str(stop_file),
                )
                for block in source_tiles
            ]
            try:
                for future in as_completed(futures):
                    future.result()
            except KeyboardInterrupt:
                stop_file.touch()
                for future in futures:
                    future.cancel()
                raise

    stop_file.unlink(missing_ok=True)
    return edge_root
