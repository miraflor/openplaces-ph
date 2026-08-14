from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Scope, bbox_sql
from .db import connect
from .sources import osm_tile_files, regular_source_tile_files
from .tiles import Tile
from .util import quote_paths, valid_parquet

SOURCE_CODE = {"fsq": 1, "overture": 2, "osm": 4}
SOURCE_PRIORITY = {"fsq": 0, "overture": 1, "osm": 2}


class UnionFindMask:
    """Compact union-find: ~10 bytes per observation plus NumPy overhead.

    mask is a bitset of source membership. A union is rejected if the two
    clusters already contain the same source, enforcing one FSQ / Overture /
    OSM observation per canonical POI.
    """

    def __init__(self, source_mask: np.ndarray):
        n = len(source_mask)
        self.parent = np.arange(n, dtype=np.int64)
        self.rank = np.zeros(n, dtype=np.uint8)
        self.mask = source_mask.astype(np.uint8, copy=True)

    def find(self, x: int) -> int:
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = int(p[x])
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.mask[ra] & self.mask[rb]:
            return False
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.mask[ra] |= self.mask[rb]
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1
        return True

    def compress_all(self) -> None:
        for i in range(len(self.parent)):
            self.parent[i] = self.find(i)


def _source_files(
    source: str,
    source_tiles: list[Tile],
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
) -> list[Path]:
    files: list[Path] = []
    for tile in source_tiles:
        if source == "fsq":
            files += regular_source_tile_files(fsq_dir, tile)
        elif source == "overture":
            files += regular_source_tile_files(overture_dir, tile)
        elif source == "osm":
            files += osm_tile_files(osm_dir, tile)
    return sorted(set(files))


def build_observations(
    scope: Scope,
    source_tiles: list[Tile],
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    """Concatenate the three normalized sources into one compact table.

    Important optimization: there is intentionally NO national DISTINCT/window
    deduplication here. Source blocks use half-open, non-overlapping tile bounds,
    and each provider has a stable source ID. Running a country-wide window sort
    merely to rediscover that fact is expensive on an old laptop.

    The raw source ID is retained, so duplicate-ID diagnostics can still be run
    later without making the normal pipeline pay for a global sort.
    """
    if valid_parquet(out_path) and not rebuild:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    parts = []
    for source in ("fsq", "overture", "osm"):
        files = _source_files(source, source_tiles, fsq_dir, overture_dir, osm_dir)
        if not files:
            continue
        code = SOURCE_CODE[source]
        priority = SOURCE_PRIORITY[source]
        parts.append(
            f"SELECT source, source_id, name, category, lon, lat, provenance, "
            f"{code}::UTINYINT AS source_code, {priority}::UTINYINT AS source_priority "
            f"FROM read_parquet({quote_paths(files)}, union_by_name=true)"
        )
    if not parts:
        raise RuntimeError("No source Parquet files were found.")

    scope_pred = bbox_sql("lon", "lat", scope.bboxes)
    tmp = out_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    con = connect(temp_dir / "observations", memory_limit=memory_limit, threads=1)
    con.execute(
        f"""
        COPY (
            WITH u AS (
                {' UNION ALL '.join(parts)}
            ), valid AS (
                SELECT *
                FROM u
                WHERE name IS NOT NULL AND trim(name) <> ''
                  AND source_id IS NOT NULL
                  AND lon IS NOT NULL AND lat IS NOT NULL
                  AND {scope_pred}
            )
            SELECT
                row_number() OVER () - 1 AS row_id,
                source, source_id, name, category, lon, lat, provenance,
                source_code, source_priority
            FROM valid
        ) TO '{tmp.as_posix()}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000
        )
        """
    )
    con.close()
    os.replace(tmp, out_path)
    return out_path

def build_ranked_edges(
    observations: Path,
    edge_root: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    if valid_parquet(out_path) and not rebuild:
        return out_path
    out_path.unlink(missing_ok=True)
    edge_files = sorted(p for p in edge_root.rglob("*.parquet") if valid_parquet(p))
    tmp = out_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)

    if not edge_files:
        schema = pa.schema([
            ("left_id", pa.int64()), ("right_id", pa.int64()),
            ("left_source", pa.string()), ("left_source_id", pa.string()),
            ("right_source", pa.string()), ("right_source_id", pa.string()),
            ("distance_m", pa.float64()), ("name_score", pa.float32()),
            ("category_score", pa.float32()), ("score", pa.float32()),
            ("independent", pa.bool_()),
        ])
        pq.write_table(pa.Table.from_arrays([pa.array([], type=f.type) for f in schema], schema=schema), tmp)
        os.replace(tmp, out_path)
        return out_path

    con = connect(temp_dir / "rank_edges", memory_limit=memory_limit, threads=1)
    con.execute(
        f"""
        COPY (
            SELECT
                l.row_id::BIGINT AS left_id,
                r.row_id::BIGINT AS right_id,
                e.*
            FROM read_parquet({quote_paths(edge_files)}, union_by_name=true) e
            JOIN read_parquet('{observations.as_posix()}') l
              ON e.left_source = l.source AND e.left_source_id = l.source_id
            JOIN read_parquet('{observations.as_posix()}') r
              ON e.right_source = r.source AND e.right_source_id = r.source_id
            ORDER BY e.score DESC, e.name_score DESC, e.distance_m ASC, e.left_source, e.left_source_id, e.right_source, e.right_source_id
        ) TO '{tmp.as_posix()}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
        )
        """
    )
    con.close()
    os.replace(tmp, out_path)
    return out_path


def cluster_edges(
    observations: Path,
    ranked_edges: Path,
    clusters_path: Path,
    state_dir: Path,
    rebuild: bool = False,
) -> Path:
    """Crash-safe, resumable greedy union-find over ranked edge row groups.

    The union-find itself is deliberately small: parent:int64 + rank:uint8 +
    source-mask:uint8 ~= 10 bytes per observation.  On an 8-GB machine it is
    safer to keep this compact state in RAM while processing one edge row group,
    then atomically replace a single ``uf_checkpoint.npz`` file.

    Why a transactional snapshot rather than directly mutating memmaps?
    Direct memmaps can be flushed by the OS before our checkpoint marker moves;
    a hard kill could therefore leave a partially advanced greedy state.  Here,
    the previously committed .npz is never modified.  If the process dies, at
    most the *current* 100k-edge Parquet row group is replayed, with identical
    ordering and therefore identical clustering.
    """
    if valid_parquet(clusters_path) and not rebuild:
        return clusters_path
    if rebuild and state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    obs_pf = pq.ParquetFile(observations)
    edge_pf = pq.ParquetFile(ranked_edges)
    n = obs_pf.metadata.num_rows
    signature = {
        "observations_size": observations.stat().st_size,
        "ranked_edges_size": ranked_edges.stat().st_size,
        "n_observations": n,
        "edge_row_groups": edge_pf.num_row_groups,
    }
    signature_json = json.dumps(signature, sort_keys=True)
    checkpoint = state_dir / "uf_checkpoint.npz"

    def save_checkpoint(parent, rank, mask, completed_rg: int) -> None:
        tmp = state_dir / "uf_checkpoint.npz.part"
        tmp.unlink(missing_ok=True)
        with open(tmp, "wb") as fh:
            # np.savez is intentionally uncompressed: much less CPU, predictable
            # RAM, and still only ~10 bytes per observation plus ZIP headers.
            np.savez(
                fh,
                parent=parent,
                rank=rank,
                mask=mask,
                completed_row_group=np.asarray([completed_rg], dtype=np.int64),
                signature=np.asarray([signature_json]),
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, checkpoint)

    parent = rank = mask = None
    completed_rg = -1
    if checkpoint.exists():
        try:
            with np.load(checkpoint, allow_pickle=False) as z:
                saved_signature = str(z["signature"][0])
                if saved_signature == signature_json:
                    parent = z["parent"].astype(np.int64, copy=True)
                    rank = z["rank"].astype(np.uint8, copy=True)
                    mask = z["mask"].astype(np.uint8, copy=True)
                    completed_rg = int(z["completed_row_group"][0])
        except Exception:
            # A .part file is never promoted, but if the committed checkpoint is
            # externally corrupted we safely rebuild it from observations.
            parent = rank = mask = None

    if parent is None:
        parent = np.arange(n, dtype=np.int64)
        rank = np.zeros(n, dtype=np.uint8)
        mask = np.zeros(n, dtype=np.uint8)
        for rg in range(obs_pf.num_row_groups):
            t = obs_pf.read_row_group(rg, columns=["row_id", "source_code"])
            ids = t["row_id"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
            codes = t["source_code"].to_numpy(zero_copy_only=False).astype(np.uint8, copy=False)
            mask[ids] = codes
        save_checkpoint(parent, rank, mask, -1)
        completed_rg = -1

    def find(x: int) -> int:
        p = parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = int(p[x])
        return x

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        # Never permit two observations from the same source in one entity.
        if mask[ra] & mask[rb]:
            return False
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        mask[ra] |= mask[rb]
        if rank[ra] == rank[rb]:
            rank[ra] += 1
        return True

    for rg in range(completed_rg + 1, edge_pf.num_row_groups):
        table = edge_pf.read_row_group(rg, columns=["left_id", "right_id"])
        left = table["left_id"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
        right = table["right_id"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
        for i in range(len(left)):
            union(int(left[i]), int(right[i]))
        # Transaction boundary: after this atomic replace, this whole row group
        # is durable. A kill before replace leaves the previous checkpoint intact.
        save_checkpoint(parent, rank, mask, rg)

    # Final path compression can simply be repeated if interrupted; all greedy
    # decisions above are already durable in the checkpoint.
    for i in range(n):
        parent[i] = find(i)

    cluster_tmp = clusters_path.with_suffix(".parquet.part")
    cluster_tmp.unlink(missing_ok=True)
    schema = pa.schema([("row_id", pa.int64()), ("cluster_root", pa.int64())])
    writer = pq.ParquetWriter(cluster_tmp, schema, compression="zstd")
    chunk = 500_000
    try:
        for start in range(0, n, chunk):
            stop = min(start + chunk, n)
            writer.write_table(pa.Table.from_arrays([
                pa.array(np.arange(start, stop, dtype=np.int64)),
                pa.array(parent[start:stop]),
            ], schema=schema))
    finally:
        writer.close()
    os.replace(cluster_tmp, clusters_path)
    return clusters_path

def build_match_edges(
    ranked_edges: Path,
    clusters: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    """Attach final-cluster membership to every threshold-accepted link."""
    if valid_parquet(out_path) and not rebuild:
        return out_path
    out_path.unlink(missing_ok=True)
    tmp = out_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    con = connect(temp_dir / "match_edges_final", memory_limit=memory_limit, threads=1)
    con.execute(f"""
        COPY (
            SELECT e.*,
                   (cl.cluster_root = cr.cluster_root) AS same_cluster_final
            FROM read_parquet('{ranked_edges.as_posix()}') e
            JOIN read_parquet('{clusters.as_posix()}') cl ON e.left_id = cl.row_id
            JOIN read_parquet('{clusters.as_posix()}') cr ON e.right_id = cr.row_id
        ) TO '{tmp.as_posix()}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
        )
    """)
    con.close()
    os.replace(tmp, out_path)
    return out_path

def build_canonical(
    observations: Path,
    clusters: Path,
    match_edges: Path,
    canonical_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    if valid_parquet(canonical_path) and not rebuild:
        return canonical_path
    canonical_path.unlink(missing_ok=True)
    tmp = canonical_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)
    con = connect(temp_dir / "canonical", memory_limit=memory_limit, threads=1, spatial=True)
    con.execute(
        f"""
        COPY (
            WITH j AS (
                SELECT o.*, c.cluster_root
                FROM read_parquet('{observations.as_posix()}') o
                JOIN read_parquet('{clusters.as_posix()}') c USING (row_id)
            ), a AS (
                SELECT
                    cluster_root,
                    median(lon) AS lon,
                    median(lat) AS lat,
                    count(DISTINCT source) AS source_count,
                    arg_min(name, source_priority) AS canonical_name,
                    arg_min(category, source_priority) AS canonical_category,
                    max(source_id) FILTER (WHERE source='fsq') AS fsq_id,
                    max(name) FILTER (WHERE source='fsq') AS fsq_name,
                    max(category) FILTER (WHERE source='fsq') AS fsq_category,
                    max(source_id) FILTER (WHERE source='overture') AS overture_id,
                    max(name) FILTER (WHERE source='overture') AS overture_name,
                    max(category) FILTER (WHERE source='overture') AS overture_category,
                    max(provenance) FILTER (WHERE source='overture') AS overture_provenance,
                    max(source_id) FILTER (WHERE source='osm') AS osm_id,
                    max(name) FILTER (WHERE source='osm') AS osm_name,
                    max(category) FILTER (WHERE source='osm') AS osm_category
                FROM j
                GROUP BY cluster_root
            ), ms AS (
                SELECT c.cluster_root, min(e.score) AS match_score_min
                FROM read_parquet('{match_edges.as_posix()}') e
                JOIN read_parquet('{clusters.as_posix()}') c ON e.left_id = c.row_id
                WHERE e.same_cluster_final
                GROUP BY c.cluster_root
            ), final AS (
                SELECT
                    CASE
                        WHEN fsq_id IS NOT NULL THEN 'fsq:' || fsq_id
                        WHEN overture_id IS NOT NULL THEN 'overture:' || overture_id
                        ELSE 'osm:' || osm_id
                    END AS canonical_id,
                    canonical_name,
                    canonical_category,
                    lon,
                    lat,
                    ST_Point(lon, lat) AS geometry,
                    source_count,
                    source_count - CASE
                        WHEN fsq_id IS NOT NULL
                         AND overture_id IS NOT NULL
                         AND regexp_matches(lower(coalesce(overture_provenance,'')), 'foursquare|\\bfsq\\b')
                        THEN 1 ELSE 0 END AS known_independent_source_count,
                    CASE source_count
                        WHEN 1 THEN 'single'
                        WHEN 2 THEN 'double'
                        WHEN 3 THEN 'triple'
                        ELSE CAST(source_count AS VARCHAR)
                    END AS evidence_tier,
                    ms.match_score_min,
                    (fsq_id IS NOT NULL AND overture_id IS NOT NULL
                     AND regexp_matches(lower(coalesce(overture_provenance,'')), 'foursquare|\\bfsq\\b'))
                        AS overture_has_foursquare_provenance,
                    fsq_id, fsq_name, fsq_category,
                    overture_id, overture_name, overture_category, overture_provenance,
                    osm_id, osm_name, osm_category
                FROM a
                LEFT JOIN ms USING (cluster_root)
            )
            SELECT * FROM final
        ) TO '{tmp.as_posix()}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
        )
        """
    )
    con.close()
    os.replace(tmp, canonical_path)
    return canonical_path


def write_summary(canonical: Path, observations: Path, match_edges: Path, out_path: Path) -> dict:
    con = duckdb.connect()
    canonical_n = con.execute(f"SELECT count(*) FROM read_parquet('{canonical.as_posix()}')").fetchone()[0]
    obs_n = con.execute(f"SELECT count(*) FROM read_parquet('{observations.as_posix()}')").fetchone()[0]
    edges_n = con.execute(f"SELECT count(*) FROM read_parquet('{match_edges.as_posix()}')").fetchone()[0]
    kept_n = con.execute(f"SELECT count(*) FROM read_parquet('{match_edges.as_posix()}') WHERE same_cluster_final").fetchone()[0]
    tiers = dict(con.execute(
        f"SELECT evidence_tier, count(*) FROM read_parquet('{canonical.as_posix()}') GROUP BY evidence_tier"
    ).fetchall())
    con.close()
    payload = {
        "observations": int(obs_n),
        "candidate_links_accepted_by_thresholds": int(edges_n),
        "accepted_links_within_final_cluster": int(kept_n),
        "canonical_pois": int(canonical_n),
        "evidence_tiers": {str(k): int(v) for k, v in tiers.items()},
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def finalize(
    root: Path,
    scope: Scope,
    source_tiles: list[Tile],
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
    edge_root: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> dict:
    from .util import atomic_json

    out = root / "data" / "output" / scope.slug
    work = root / "data" / "work" / scope.slug / "finalize"
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    edge_cfg = edge_root / "_config.json"
    dependency_payload = {
        "edge_config": json.loads(edge_cfg.read_text(encoding="utf-8")) if edge_cfg.exists() else None,
        "source_tiles": [t.key for t in source_tiles],
    }
    dep_path = work / "_dependencies.json"
    if dep_path.exists() and not rebuild:
        try:
            previous = json.loads(dep_path.read_text(encoding="utf-8"))
        except Exception:
            previous = None
        if previous != dependency_payload:
            print("[finalize] upstream configuration changed; rebuilding final checkpoints")
            rebuild = True

    if rebuild:
        for p in (out / "observations.parquet", out / "match_edges.parquet", out / "canonical_pois.parquet",
                  work / "ranked_edges.parquet", work / "clusters.parquet"):
            p.unlink(missing_ok=True)
        uf_state = work / "uf_state"
        if uf_state.exists():
            shutil.rmtree(uf_state)
    atomic_json(dep_path, dependency_payload)

    observations = build_observations(
        scope, source_tiles, fsq_dir, overture_dir, osm_dir,
        out / "observations.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    ranked = build_ranked_edges(
        observations, edge_root, work / "ranked_edges.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    clusters = cluster_edges(
        observations, ranked, work / "clusters.parquet", work / "uf_state", rebuild=rebuild,
    )
    match_edges = build_match_edges(
        ranked, clusters, out / "match_edges.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    canonical = build_canonical(
        observations, clusters, match_edges, out / "canonical_pois.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    return write_summary(canonical, observations, match_edges, out / "summary.json")
