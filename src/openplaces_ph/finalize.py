"""Rank accepted links, cluster observations, and build the canonical layer.

Three deliberate design points, all aimed at an 8 GB laptop with an aging disk:

1. **Only the pair travels through the national sort.** Greedy clustering reads
   nothing but ``(left_id, right_id)`` in rank order, so the wide edge
   attributes (source ids, scores, distances) never enter the ORDER BY payload.
2. **Row ids are assigned by a streaming second pass**, not by a window
   operator that has to materialize the whole national observation table.
3. **Union-find state is checkpointed on a time budget**, not once per edge row
   group. Replay is deterministic, so a cheaper cadence is equally crash-safe.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from array import array
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .config import Scope, bbox_sql
from .db import connect
from .matching import EARTH_RADIUS_M, MANIFEST_NAME, match_checkpoints_complete
from .snapshot import PipelineStateError, source_snapshot
from .sources import (
    CRS84,
    clear_tile_file_caches,
    osm_tile_files,
    overture_fsq_provenance_sql,
    overture_license_sql,
    regular_source_tile_files,
)
from .tiles import Tile
from .util import atomic_json, quote_paths, read_json, valid_parquet

SOURCE_CODE = {"fsq": 1, "overture": 2, "osm": 4}
SOURCE_PRIORITY = {"fsq": 0, "overture": 1, "osm": 2}

OBSERVATION_COLUMNS = (
    "source", "source_id", "name", "category", "lon", "lat",
    "provenance", "upstream_license", "source_code", "source_priority",
)

EDGE_ID_SCHEMA = pa.schema([
    ("left_id", pa.int64()), ("right_id", pa.int64()),
    ("left_source", pa.string()), ("left_source_id", pa.string()),
    ("right_source", pa.string()), ("right_source_id", pa.string()),
    ("distance_m", pa.float64()), ("name_score", pa.float32()),
    ("category_score", pa.float32()), ("score", pa.float32()),
    ("independent", pa.bool_()),
])

RANKED_PAIR_SCHEMA = pa.schema([("left_id", pa.int64()), ("right_id", pa.int64())])

# Bump when finalization logic changes so existing final checkpoints rebuild.
# 4: Overture licences are re-derived here from provenance (dataset names only).
FINALIZE_VERSION = 4

# One transactional snapshot per minute of clustering work. The previous
# per-row-group cadence wrote ~10 bytes x n_observations for every 100k edges,
# which on a national run is gigabytes of fsync'd I/O for no extra safety:
# replaying a few row groups is deterministic and costs seconds.
CHECKPOINT_SECONDS = 60.0


def assert_geoparquet(path: Path) -> None:
    """Fail loudly if a file advertised as GeoParquet lost its geo metadata.

    ``geometry`` values without the GeoParquet metadata key still *look* like a
    binary column to many readers.  That kind of silent downgrade is dangerous
    for a public dataset, so finalization checks the footer before promoting the
    temporary file to the canonical filename.
    """
    metadata = pq.ParquetFile(path).schema_arrow.metadata or {}
    if b"geo" not in metadata:
        raise RuntimeError(
            "Canonical output has a geometry column but no GeoParquet 'geo' metadata. "
            "This usually means the DuckDB spatial/GeoParquet writer behaviour changed."
        )


def _add_geo_metadata_if_empty(path: Path) -> None:
    """Give an empty canonical file the GeoParquet metadata DuckDB leaves out.

    DuckDB 1.5.5 writes the ``geo`` key only when at least one geometry was
    written, so an area without any POI failed :func:`assert_geoparquet`.
    GeoParquet 1.0 allows an empty ``geometry_types`` list ("unknown"), and an
    absent ``crs`` means OGC:CRS84, which is the contract here. Non-empty files
    are left alone, so the strict check still detects a real writer change.
    """
    parquet = pq.ParquetFile(path)
    if parquet.metadata.num_rows != 0 or b"geo" in (parquet.schema_arrow.metadata or {}):
        return
    table = parquet.read()
    metadata = dict(table.schema.metadata or {})
    metadata[b"geo"] = json.dumps({
        "version": "1.0.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []}},
    }).encode("utf-8")
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="zstd")


class UnionFindMask:
    """Compact union-find: ~10 bytes per observation plus NumPy overhead.

    mask is a bitset of source membership. A union is rejected if the two
    clusters already contain the same source, enforcing one FSQ / Overture /
    OSM observation per canonical POI.

    This class documents (and unit-tests) the invariant. The national run uses
    the same algorithm with ``array('I')`` + ``bytearray`` storage inside
    :func:`cluster_edges`: Python-scalar indexing remains cheap, but every parent
    occupies four bytes rather than a full Python ``int`` object.
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
        self.parent = compress_parents(self.parent)


def compress_parents(parent: np.ndarray) -> np.ndarray:
    """Vectorised full path compression by pointer doubling.

    Equivalent to ``for i in range(n): parent[i] = find(i)`` but performed in
    O(log depth) NumPy passes instead of n interpreted iterations.
    """
    p = np.asarray(parent, dtype=np.int64)
    while True:
        q = p[p]
        if np.array_equal(q, p):
            return p
        p = q


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

    Two important choices:

    * There is intentionally NO national DISTINCT/window deduplication. Source
      blocks use half-open, non-overlapping tile bounds and each provider has a
      stable source ID; a country-wide sort merely to rediscover that is
      expensive on constrained hardware.
    * ``row_id`` is assigned by writing the filtered table once and re-reading
      it with ``file_row_number``. ``row_number() OVER ()`` looks cheaper but
      forces DuckDB's window operator to materialize every national row inside
      a 1 GB budget, which is exactly the spill this pipeline exists to avoid.
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
            f"SELECT source, source_id, name, category, lon, lat, provenance, upstream_license, "
            f"{code}::UTINYINT AS source_code, {priority}::UTINYINT AS source_priority "
            f"FROM read_parquet({quote_paths(files)}, union_by_name=true)"
        )
    if not parts:
        raise RuntimeError("No source Parquet files were found.")

    scope_pred = bbox_sql("lon", "lat", scope.bboxes)
    # The Overture licence is re-derived from provenance here rather than
    # trusted from the source tile. Tiles keep the label computed when they
    # were downloaded; deriving it at finalization means a corrected licence
    # rule applies at the next finalize, without downloading Overture again.
    projection = ", ".join(
        f"CASE WHEN source = 'overture' THEN {overture_license_sql('provenance')} "
        "ELSE upstream_license END AS upstream_license"
        if column == "upstream_license" else column
        for column in OBSERVATION_COLUMNS
    )
    stage = out_path.with_suffix(".stage.parquet")
    tmp = out_path.with_suffix(".parquet.part")
    stage.unlink(missing_ok=True)
    tmp.unlink(missing_ok=True)

    con = connect(temp_dir / "observations", memory_limit=memory_limit, threads=1)
    try:
        # Pass 1: filter and project. Pure streaming, no ordering, no window.
        con.execute(
            f"""
            COPY (
                SELECT {projection}
                FROM ( {' UNION ALL '.join(parts)} )
                WHERE name IS NOT NULL AND trim(name) <> ''
                  AND source_id IS NOT NULL
                  AND lon IS NOT NULL AND lat IS NOT NULL
                  AND {scope_pred}
            ) TO '{stage.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000
            )
            """
        )
        # Pass 2: attach a dense 0..n-1 id from the physical row order.
        con.execute(
            f"""
            COPY (
                SELECT file_row_number::BIGINT AS row_id, {', '.join(OBSERVATION_COLUMNS)}
                FROM read_parquet('{stage.as_posix()}', file_row_number=true)
            ) TO '{tmp.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000
            )
            """
        )
    finally:
        con.close()

    os.replace(tmp, out_path)
    stage.unlink(missing_ok=True)
    return out_path


def build_edge_ids(
    observations: Path,
    edge_root: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    """Attach dense observation ids to every accepted link. No ordering.

    Splitting this from the ranking step is what keeps the wide string columns
    out of the national sort payload.
    """
    if valid_parquet(out_path) and not rebuild:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    tmp = out_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)

    edge_files = sorted(p for p in edge_root.rglob("*.parquet") if valid_parquet(p))
    if not edge_files:
        pq.write_table(
            pa.Table.from_arrays(
                [pa.array([], type=f.type) for f in EDGE_ID_SCHEMA], schema=EDGE_ID_SCHEMA
            ),
            tmp,
            compression="zstd",
        )
        os.replace(tmp, out_path)
        return out_path

    con = connect(temp_dir / "edge_ids", memory_limit=memory_limit, threads=1)
    try:
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
            ) TO '{tmp.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
            )
            """
        )
    finally:
        con.close()
    os.replace(tmp, out_path)

    # Every accepted edge must resolve to exactly one observation on each side.
    # A count mismatch means a source id vanished from ``observations`` *or* was
    # duplicated there.  Silently dropping/duplicating such edges would alter the
    # national clustering, so fail here while the cause is still local and clear.
    expected_edges = sum(pq.ParquetFile(path).metadata.num_rows for path in edge_files)
    resolved_edges = pq.ParquetFile(out_path).metadata.num_rows
    if resolved_edges != expected_edges:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Accepted-edge resolution mismatch: {expected_edges} source edges became "
            f"{resolved_edges} id-resolved edges. Check duplicate/missing source IDs."
        )
    return out_path


def build_ranked_pairs(
    edge_ids: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    rebuild: bool = False,
) -> Path:
    """Sort links strongest-first, carrying only the 16-byte id pair.

    Tie-breaking stays on the upstream source ids (not on ``row_id``) so a
    ``--rebuild-finalize`` that reassigns dense ids still produces byte-identical
    ranking, and therefore identical clusters.
    """
    if valid_parquet(out_path) and not rebuild:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)
    tmp = out_path.with_suffix(".parquet.part")
    tmp.unlink(missing_ok=True)

    if pq.ParquetFile(edge_ids).metadata.num_rows == 0:
        pq.write_table(
            pa.Table.from_arrays(
                [pa.array([], type=f.type) for f in RANKED_PAIR_SCHEMA],
                schema=RANKED_PAIR_SCHEMA,
            ),
            tmp,
            compression="zstd",
        )
        os.replace(tmp, out_path)
        return out_path

    con = connect(temp_dir / "ranked_pairs", memory_limit=memory_limit, threads=1)
    try:
        con.execute(
            f"""
            COPY (
                SELECT left_id, right_id
                FROM read_parquet('{edge_ids.as_posix()}')
                ORDER BY score DESC, name_score DESC, distance_m ASC,
                         left_source, left_source_id, right_source, right_source_id
            ) TO '{tmp.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
            )
            """
        )
    finally:
        con.close()
    os.replace(tmp, out_path)
    return out_path


def cluster_edges(
    observations: Path,
    ranked_pairs: Path,
    clusters_path: Path,
    state_dir: Path,
    rebuild: bool = False,
    checkpoint_seconds: float = CHECKPOINT_SECONDS,
) -> Path:
    """Crash-safe, resumable greedy union-find over ranked edge row groups.

    Working-memory state is intentionally compact:

    ``parent``
        ``array('I')`` = 4 bytes per observation on the supported Windows/Linux
        platforms.  This behaves like a Python sequence in the hot loop without
        allocating one heavyweight Python ``int`` object per observation.
    ``rank``
        one byte per observation.
    ``mask``
        one byte per observation; bits 1/2/4 mean FSQ/Overture/OSM.

    The core state is therefore about **6 bytes per observation**, not merely
    6 bytes on disk.  That distinction is important on the 8 GB target laptop.

    Why a transactional snapshot rather than directly mutating memmaps?
    Direct memmaps can be flushed by the OS before our checkpoint marker moves;
    a hard kill could therefore leave a partially advanced greedy state. Here,
    the previously committed ``.npz`` is never modified. If the process dies,
    only the edge row groups since the last snapshot are replayed, in the same
    deterministic order.
    """
    if valid_parquet(clusters_path) and not rebuild:
        return clusters_path
    if rebuild and state_dir.exists():
        shutil.rmtree(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    # ``array('I')`` is a C unsigned int.  CPython uses four bytes for it on the
    # x86-64 Windows/Linux systems supported by this project.  Refuse an exotic
    # platform rather than silently losing the memory guarantee or overflowing.
    if array("I").itemsize != 4:
        raise RuntimeError("OpenPlaces PH requires a 4-byte C unsigned int for compact clustering state.")

    obs_pf = pq.ParquetFile(observations)
    edge_pf = pq.ParquetFile(ranked_pairs)
    n = obs_pf.metadata.num_rows
    if n >= 2**32:
        raise RuntimeError(
            "The compact union-find supports fewer than 2^32 observations. "
            "A Philippine run should be far below this; if you reached it, the "
            "clustering representation needs to be redesigned rather than widened silently."
        )

    signature = {
        "observations_size": observations.stat().st_size,
        "ranked_pairs_size": ranked_pairs.stat().st_size,
        "n_observations": n,
        "edge_row_groups": edge_pf.num_row_groups,
        "state_version": 3,
    }
    signature_json = json.dumps(signature, sort_keys=True)
    checkpoint = state_dir / "uf_checkpoint.npz"

    def parent_view(parent_array: array) -> np.ndarray:
        """Zero-copy NumPy view over the compact 32-bit parent buffer."""
        return np.frombuffer(parent_array, dtype=np.uint32)

    def save_checkpoint(parent_array: array, rank_ba: bytearray, mask_ba: bytearray,
                        completed_rg: int) -> None:
        """Atomically snapshot only the compact buffers plus a progress marker."""
        tmp = state_dir / "uf_checkpoint.npz.part"
        tmp.unlink(missing_ok=True)
        with open(tmp, "wb") as fh:
            # Uncompressed on purpose: the file is only ~6 bytes per observation,
            # and compression would spend CPU and transient RAM on an aging laptop.
            # ``frombuffer`` is zero-copy, so snapshot preparation does not build
            # a second Python-sized representation of the national parent vector.
            np.savez(
                fh,
                parent=parent_view(parent_array),
                rank=np.frombuffer(rank_ba, dtype=np.uint8),
                mask=np.frombuffer(mask_ba, dtype=np.uint8),
                completed_row_group=np.asarray([completed_rg], dtype=np.int64),
                signature=np.asarray([signature_json]),
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, checkpoint)

    parent: array | None = None
    rank: bytearray | None = None
    mask: bytearray | None = None
    completed_rg = -1

    if checkpoint.exists():
        try:
            with np.load(checkpoint, allow_pickle=False) as z:
                if str(z["signature"][0]) == signature_json:
                    saved_parent = np.ascontiguousarray(z["parent"], dtype=np.uint32)
                    parent = array("I")
                    # ``array.frombytes`` accepts a byte-view, avoiding a list of
                    # millions of temporary Python integers during resume.
                    parent.frombytes(memoryview(saved_parent).cast("B"))
                    rank = bytearray(np.ascontiguousarray(z["rank"], dtype=np.uint8).tobytes())
                    mask = bytearray(np.ascontiguousarray(z["mask"], dtype=np.uint8).tobytes())
                    completed_rg = int(z["completed_row_group"][0])
        except Exception:
            # A .part file is never promoted, but if the committed checkpoint is
            # externally corrupted we safely rebuild it from observations.
            parent = rank = mask = None

    if parent is None:
        # Build 0,1,2,...,n-1 once as uint32 and copy its raw bytes into the compact
        # Python array.  Peak temporary memory here is only ~4 bytes/observation.
        initial = np.arange(n, dtype=np.uint32)
        parent = array("I")
        parent.frombytes(memoryview(initial).cast("B"))
        del initial

        rank = bytearray(n)
        mask = bytearray(n)

        # ``row_id`` is dense, so we can fill the mask in streaming row groups.
        # ``mask_view`` is a *zero-copy* NumPy view over the bytearray: vectorised
        # assignment is fast, but we still keep only one national mask buffer.
        mask_view = np.frombuffer(mask, dtype=np.uint8)
        for rg in range(obs_pf.num_row_groups):
            table = obs_pf.read_row_group(rg, columns=["row_id", "source_code"])
            ids = table["row_id"].to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
            codes = table["source_code"].to_numpy(zero_copy_only=False).astype(np.uint8, copy=False)
            mask_view[ids] = codes
        del mask_view

        save_checkpoint(parent, rank, mask, -1)
        completed_rg = -1

    # Mypy/reader aid: from this point onward all three buffers are present.
    assert parent is not None and rank is not None and mask is not None

    def union(a: int, b: int) -> None:
        """Join two components unless that would duplicate a source layer."""
        ra = a
        while parent[ra] != ra:
            parent[ra] = parent[parent[ra]]  # path halving
            ra = parent[ra]

        rb = b
        while parent[rb] != rb:
            parent[rb] = parent[parent[rb]]
            rb = parent[rb]

        if ra == rb:
            return

        # The bit mask is the central modelling constraint.  If both components
        # already contain (say) an FSQ observation, merging them would create one
        # canonical POI containing two FSQ establishments, so reject the edge.
        if mask[ra] & mask[rb]:
            return

        # Union by rank keeps trees shallow, which makes every later ``find``
        # cheaper. Rank never approaches 255 for a realistic dataset, hence one byte.
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        mask[ra] |= mask[rb]
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    last_saved = time.monotonic()
    last_rg = completed_rg
    for rg in range(completed_rg + 1, edge_pf.num_row_groups):
        table = edge_pf.read_row_group(rg, columns=["left_id", "right_id"])

        # Row groups are deliberately only 100k pairs. Converting these two small
        # Arrow columns to Python lists is much faster in the scalar union loop and
        # costs only a bounded, short-lived amount of memory.
        left = table["left_id"].to_pylist()
        right = table["right_id"].to_pylist()
        for a, b in zip(left, right):
            union(a, b)
        last_rg = rg

        # Transaction boundary: after this atomic replace, every row group up to
        # and including ``rg`` is durable.  Between snapshots, replay is safe.
        if time.monotonic() - last_saved >= checkpoint_seconds:
            save_checkpoint(parent, rank, mask, rg)
            last_saved = time.monotonic()

    if last_rg > completed_rg:
        save_checkpoint(parent, rank, mask, last_rg)

    # Final path compression happens directly in the compact parent buffer.
    # ``p[p]`` creates one temporary uint32 vector (~4 bytes/observation), so peak
    # memory here is about 10 bytes/observation for parent+temp+rank+mask rather
    # than converting the whole structure to int64.
    p = parent_view(parent)
    while True:
        q = p[p]
        if np.array_equal(q, p):
            break
        p[:] = q
        del q

    cluster_tmp = clusters_path.with_suffix(".parquet.part")
    cluster_tmp.unlink(missing_ok=True)
    schema = pa.schema([("row_id", pa.int64()), ("cluster_root", pa.int64())])
    writer = pq.ParquetWriter(cluster_tmp, schema, compression="zstd")
    chunk = 500_000
    try:
        for start in range(0, n, chunk):
            stop = min(start + chunk, n)
            # Cast only the current chunk to int64 for the public Parquet schema;
            # the national in-memory representation stays uint32 throughout.
            writer.write_table(pa.Table.from_arrays([
                pa.array(np.arange(start, stop, dtype=np.int64)),
                pa.array(p[start:stop].astype(np.int64, copy=False)),
            ], schema=schema))
    finally:
        writer.close()
    os.replace(cluster_tmp, clusters_path)
    return clusters_path


def build_match_edges(
    edge_ids: Path,
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
    try:
        con.execute(f"""
            COPY (
                SELECT e.*,
                       (cl.cluster_root = cr.cluster_root) AS same_cluster_final
                FROM read_parquet('{edge_ids.as_posix()}') e
                JOIN read_parquet('{clusters.as_posix()}') cl ON e.left_id = cl.row_id
                JOIN read_parquet('{clusters.as_posix()}') cr ON e.right_id = cr.row_id
            ) TO '{tmp.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
            )
        """)
    finally:
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
    fsq_in_overture = overture_fsq_provenance_sql("overture_provenance")
    con = connect(temp_dir / "canonical", memory_limit=memory_limit, threads=1, spatial=True)
    try:
        con.execute(
            f"""
            COPY (
                WITH j AS (
                    SELECT o.*, c.cluster_root
                    FROM read_parquet('{observations.as_posix()}') o
                    JOIN read_parquet('{clusters.as_posix()}') c USING (row_id)
                ), pts AS (
                    SELECT cluster_root, row_id, lon, lat FROM j
                ), dia AS (
                    -- Clusters hold at most one observation per source, so this
                    -- self-join emits at most 3 pairs per cluster. It exposes
                    -- clusters that were only ever joined transitively: two
                    -- members can sit up to 2 x max_distance apart even though
                    -- no direct link between them was ever accepted.
                    SELECT p1.cluster_root,
                           max(2 * {EARTH_RADIUS_M} * asin(sqrt(
                               pow(sin(radians(p2.lat - p1.lat) / 2), 2)
                               + cos(radians(p1.lat)) * cos(radians(p2.lat))
                               * pow(sin(radians(p2.lon - p1.lon) / 2), 2)
                           ))) AS cluster_max_pair_distance_m
                    FROM pts p1
                    JOIN pts p2
                      ON p1.cluster_root = p2.cluster_root AND p1.row_id < p2.row_id
                    GROUP BY p1.cluster_root
                ), a AS (
                    SELECT
                        cluster_root,
                        median(lon) AS lon,
                        median(lat) AS lat,
                        count(DISTINCT source_code) AS source_count,
                        arg_min(name, source_priority) AS canonical_name,
                        arg_min(category, source_priority) AS canonical_category,
                        max(source_id) FILTER (WHERE source='fsq') AS fsq_id,
                        max(name) FILTER (WHERE source='fsq') AS fsq_name,
                        max(category) FILTER (WHERE source='fsq') AS fsq_category,
                        max(upstream_license) FILTER (WHERE source='fsq') AS fsq_license,
                        max(source_id) FILTER (WHERE source='overture') AS overture_id,
                        max(name) FILTER (WHERE source='overture') AS overture_name,
                        max(category) FILTER (WHERE source='overture') AS overture_category,
                        max(provenance) FILTER (WHERE source='overture') AS overture_provenance,
                        max(upstream_license) FILTER (WHERE source='overture') AS overture_license,
                        max(source_id) FILTER (WHERE source='osm') AS osm_id,
                        max(name) FILTER (WHERE source='osm') AS osm_name,
                        max(category) FILTER (WHERE source='osm') AS osm_category,
                        max(upstream_license) FILTER (WHERE source='osm') AS osm_license
                    FROM j
                    GROUP BY cluster_root
                ), ms AS (
                    SELECT c.cluster_root,
                           min(e.score) AS match_score_min,
                           count(*) AS internal_link_count
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
                        ST_SetCRS(ST_Point(lon, lat), '{CRS84}') AS geometry,
                        source_count,
                        source_count - CASE
                            WHEN fsq_id IS NOT NULL
                             AND overture_id IS NOT NULL
                             AND {fsq_in_overture}
                            THEN 1 ELSE 0 END AS known_independent_source_count,
                        CASE source_count
                            WHEN 1 THEN 'single'
                            WHEN 2 THEN 'double'
                            WHEN 3 THEN 'triple'
                            ELSE CAST(source_count AS VARCHAR)
                        END AS evidence_tier,
                        ms.match_score_min,
                        dia.cluster_max_pair_distance_m,
                        -- A 3-source cluster held together by only 2 accepted
                        -- links was completed by transitivity, not by direct
                        -- evidence between every pair.
                        (source_count > 2
                         AND coalesce(ms.internal_link_count, 0)
                             < source_count * (source_count - 1) / 2) AS completed_transitively,
                        (fsq_id IS NOT NULL AND overture_id IS NOT NULL
                         AND {fsq_in_overture})
                            AS overture_has_foursquare_provenance,
                        fsq_id, fsq_name, fsq_category, fsq_license,
                        overture_id, overture_name, overture_category, overture_provenance, overture_license,
                        osm_id, osm_name, osm_category, osm_license
                    FROM a
                    LEFT JOIN ms USING (cluster_root)
                    LEFT JOIN dia USING (cluster_root)
                )
                SELECT * FROM final
            ) TO '{tmp.as_posix()}' (
                FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
            )
            """
        )
    finally:
        con.close()

    # Validate the public contract *before* the temporary file becomes durable.
    # DuckDB 1.5.5 writes GeoParquet metadata when a CRS-aware GEOMETRY column is
    # exported with the spatial extension loaded.  If that ever changes, stop
    # rather than publishing an ambiguously typed binary geometry column.
    _add_geo_metadata_if_empty(tmp)
    assert_geoparquet(tmp)
    os.replace(tmp, canonical_path)
    return canonical_path


def write_summary(
    canonical: Path,
    observations: Path,
    match_edges: Path,
    out_path: Path,
    temp_dir: Path,
    memory_limit: str,
    sources: dict | None = None,
) -> dict:
    """Summarize the run using the same constrained DuckDB policy as every
    other stage, in two passes instead of five separate table scans.

    ``sources`` is the source snapshot the outputs were built from. Recording
    it here is step 1 of the publication checklist in DATA_LICENSES.md.
    """
    con = connect(temp_dir / "summary", memory_limit=memory_limit, threads=1)
    try:
        obs_n, edges_n, kept_n = con.execute(
            f"""
            SELECT
                (SELECT count(*) FROM read_parquet('{observations.as_posix()}')),
                (SELECT count(*) FROM read_parquet('{match_edges.as_posix()}')),
                (SELECT count(*) FILTER (WHERE same_cluster_final)
                 FROM read_parquet('{match_edges.as_posix()}'))
            """
        ).fetchone()
        canonical_n, transitive_n, tiers = con.execute(
            f"""
            SELECT count(*),
                   count(*) FILTER (WHERE completed_transitively),
                   map_from_entries(list(DISTINCT (evidence_tier, tier_n)))
            FROM (
                SELECT evidence_tier, completed_transitively,
                       count(*) OVER (PARTITION BY evidence_tier) AS tier_n
                FROM read_parquet('{canonical.as_posix()}')
            )
            """
        ).fetchone()
    finally:
        con.close()

    payload = {
        "observations": int(obs_n),
        "candidate_links_accepted_by_thresholds": int(edges_n),
        "accepted_links_within_final_cluster": int(kept_n),
        "canonical_pois": int(canonical_n),
        "canonical_pois_completed_transitively": int(transitive_n),
        # map_from_entries() returns NULL for an empty canonical layer.
        "evidence_tiers": {str(k): int(v) for k, v in dict(tiers or {}).items()},
    }
    if sources is not None:
        payload["sources"] = sources
    atomic_json(out_path, payload)
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
    out = root / "data" / "output" / scope.slug
    work = root / "data" / "work" / scope.slug / "finalize"

    # Finalization must consume exactly one snapshot: the current, complete
    # sources *and* match shards built from those same sources. Each check
    # below used to be missing, so an interrupted or stale upstream stage
    # produced a plausible-looking but partial canonical layer.
    clear_tile_file_caches()
    snapshot = source_snapshot(root, source_tiles, fsq_dir, overture_dir, osm_dir)
    edge_cfg = read_json(edge_root / MANIFEST_NAME)
    if edge_cfg is None:
        raise PipelineStateError("Match checkpoints are missing. Run `openplaces --only match` first.")
    if edge_cfg.get("sources") != snapshot:
        reason = (
            "were written by <= 0.2.0, before source snapshots were recorded"
            if "sources" not in edge_cfg
            else "were built from a different source snapshot than the current sources"
        )
        raise PipelineStateError(
            f"Match checkpoints {reason}. Run `openplaces --only match` (it rebuilds them) "
            "before finalizing."
        )
    if not match_checkpoints_complete(edge_root, validate_parquet=True):
        raise PipelineStateError(
            "Matching has not finished for the current configuration and sources. "
            "Run `openplaces --only match` to complete it before finalizing."
        )

    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    # ``edge_config`` contains the source snapshot, so a source refresh always
    # changes this payload and rebuilds every final checkpoint.
    dependency_payload = {
        "edge_config": edge_cfg,
        "source_tiles": [t.key for t in source_tiles],
        "finalize_version": FINALIZE_VERSION,
    }
    dep_path = work / "_dependencies.json"
    previous = read_json(dep_path)
    if not rebuild:
        if previous is None:
            if any(p.exists() for p in (out / "observations.parquet", work / "edge_ids.parquet",
                                        work / "clusters.parquet", out / "canonical_pois.parquet")):
                print("[finalize] final checkpoints have no readable dependency record; rebuilding them")
                rebuild = True
        elif previous != dependency_payload:
            print("[finalize] upstream configuration changed; rebuilding final checkpoints")
            rebuild = True

    if rebuild:
        for p in (out / "observations.parquet", out / "match_edges.parquet",
                  out / "canonical_pois.parquet",
                  work / "edge_ids.parquet", work / "ranked_pairs.parquet",
                  work / "clusters.parquet",
                  # legacy 0.1.0 artefact
                  work / "ranked_edges.parquet"):
            p.unlink(missing_ok=True)
        uf_state = work / "uf_state"
        if uf_state.exists():
            shutil.rmtree(uf_state)
    atomic_json(dep_path, dependency_payload)

    observations = build_observations(
        scope, source_tiles, fsq_dir, overture_dir, osm_dir,
        out / "observations.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    edge_ids = build_edge_ids(
        observations, edge_root, work / "edge_ids.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    ranked = build_ranked_pairs(
        edge_ids, work / "ranked_pairs.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    clusters = cluster_edges(
        observations, ranked, work / "clusters.parquet", work / "uf_state", rebuild=rebuild,
    )
    match_edges = build_match_edges(
        edge_ids, clusters, out / "match_edges.parquet", temp_dir, memory_limit, rebuild=rebuild,
    )
    canonical = build_canonical(
        observations, clusters, match_edges, out / "canonical_pois.parquet",
        temp_dir, memory_limit, rebuild=rebuild,
    )
    return write_summary(
        canonical, observations, match_edges, out / "summary.json", temp_dir, memory_limit,
        sources=snapshot,
    )
