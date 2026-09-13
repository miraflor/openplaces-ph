"""Command-line orchestration for the national resumable pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_area_config, resolve_scope
from .finalize import finalize
from .matching import MatchConfig, match_checkpoints_complete, prepare_matches
from .snapshot import PipelineStateError, dir_release, pinned_release
from .sources import (
    check_external_tools,
    filter_tiles_to_boundary,
    pin_overture_release,
    prepare_foursquare,
    prepare_osm,
    prepare_overture,
    prepare_overture_boundary,
    source_tiles_for_scope,
)
from .tiles import child_tiles, intersects
from .util import free_gb, valid_parquet


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Low-resource-friendly, crash-resumable triangulation of Philippine "
            "POIs from Foursquare, Overture, and OSM."
        )
    )

    # The code still retains named-area support for cheap testing, but the
    # intended production command is simply --scope philippines (the default).
    p.add_argument("--scope", default="philippines", choices=["philippines"])
    p.add_argument("--areas", nargs="+", help="Optional test areas from config/areas.yml.")
    p.add_argument(
        "--only", choices=["all", "sources", "match", "finalize"], default="all",
        help="Run one stage only. Re-running --only all is also safe and resumable.",
    )

    # i7-7700HQ = 4 physical / 8 logical cores, but old thermal hardware plus
    # constrained RAM means maximum thread count is not optimal. Parallelism is
    # deliberately asymmetric: FSQ can overlap remote latency; Overture already
    # does internal I/O readahead; matching is CPU/disk-heavy and stays serial.
    p.add_argument(
        "--overture-workers", type=int, default=1,
        help=(
            "Concurrent Overture 1-degree downloads. Default 1 because the official "
            "streaming reader already performs internal I/O readahead."
        ),
    )
    p.add_argument(
        "--fsq-workers", type=int, default=2,
        help=(
            "Concurrent Foursquare bbox queries. Default 2: each DuckDB worker itself "
            "uses one thread, so two workers can overlap remote latency without using all cores."
        ),
    )
    p.add_argument(
        "--match-workers", type=int, default=1,
        help="Parallel match workers. Default 1 for the low-resource profile.",
    )

    # Reference target: an aging Windows laptop with only 8 GB RAM.  The pipeline
    # intentionally claims a small, predictable fraction of that memory.  The
    # remaining headroom is for Windows, antivirus, filesystem cache, Python,
    # browser/editor processes, and the less predictable I/O/thermal behaviour of
    # old hardware.  Faster machines may opt into more workers explicitly.
    p.add_argument("--worker-memory", default="512MB", help="DuckDB cap per parallel worker.")
    p.add_argument("--main-memory", default="1GB", help="DuckDB cap for national single-process stages.")
    p.add_argument("--temp-dir", help="DuckDB spill directory. Prefer an SSD with >=20 GB free.")

    p.add_argument("--source-tile-deg", type=float, default=1.0, help="Remote/checkpoint block size.")
    p.add_argument("--match-tile-deg", type=float, default=0.25, help="Matching checkpoint size.")
    p.add_argument(
        "--max-distance", type=float, default=120.0,
        help=(
            "Maximum cross-source POI distance in meters. The spatial blocking "
            "grid and the name-score ladder are both derived from this value, "
            "so non-default settings stay internally consistent."
        ),
    )

    p.add_argument(
        "--refresh-sources", action="store_true",
        help=(
            "Pin the newest upstream releases. Only sources whose release changed are "
            "downloaded again; matching and final outputs rebuild automatically when the "
            "source snapshot changes. An interrupted refresh continues on the next run."
        ),
    )
    p.add_argument("--rebuild-match", action="store_true", help="Discard and recompute match checkpoints only.")
    p.add_argument("--rebuild-finalize", action="store_true", help="Rebuild final clustering/output only.")
    p.add_argument(
        "--keep-intermediates", action="store_true",
        help="Keep large OSM/Overture temporary files after successful normalization.",
    )
    p.add_argument("--status", action="store_true", help="Show durable progress and exit.")
    return p


def _paths(root: Path, scope_slug: str, source_tile_deg: float) -> tuple[Path, Path, Path]:
    tag = str(source_tile_deg).replace(".", "p")
    osm = root / "data" / "cache" / "osm" / f"tiles_{tag}deg"
    fsq = root / "data" / "sources" / scope_slug / "fsq"
    overture = root / "data" / "sources" / scope_slug / "overture"
    return fsq, overture, osm


def _status(root: Path, scope, source_tiles, source_tile_deg: float, match_tile_deg: float) -> None:
    fsq, overture, osm = _paths(root, scope.slug, source_tile_deg)
    fsq_n = sum(valid_parquet(fsq / f"{t.key}.parquet") for t in source_tiles)
    ov_n = sum(valid_parquet(overture / f"{t.key}.parquet") for t in source_tiles)
    osm_ok = (osm / "_SUCCESS.json").exists()

    expected_match_tiles = 0
    for block in source_tiles:
        for child in child_tiles(block, match_tile_deg):
            if any(intersects(child.bbox, bbox) for bbox in scope.bboxes):
                expected_match_tiles += 1

    edge_root = root / "data" / "work" / scope.slug / "edges"
    edge_n = sum(1 for p in edge_root.rglob("*.parquet") if valid_parquet(p)) if edge_root.exists() else 0
    expected_edges = expected_match_tiles * 3

    out = root / "data" / "output" / scope.slug
    def releases(source: str, directory: Path) -> str:
        pinned, bound = pinned_release(root, source), dir_release(directory)
        if pinned is None:
            return "no release pinned yet"
        if bound is None or bound == pinned:
            return f"pinned {pinned}"
        return f"pinned {pinned}, tiles still from {bound} (refresh not finished)"

    print(f"Scope: {scope.name}")
    print(f"OSM normalized cache: {'ready' if osm_ok else 'not ready'}")
    print(f"Overture 1° blocks: {ov_n}/{len(source_tiles)} ({releases('overture', overture)})")
    print(f"Foursquare 1° blocks: {fsq_n}/{len(source_tiles)} ({releases('fsq', fsq)})")
    print(f"Match checkpoints: {edge_n}/{expected_edges}"
          f"{' (complete)' if match_checkpoints_complete(edge_root) else ''}")
    for name in ("observations.parquet", "match_edges.parquet", "canonical_pois.parquet", "summary.json"):
        print(f"{name}: {'ready' if (out / name).exists() else 'not ready'}")


def main() -> None:
    args = build_parser().parse_args()

    if args.overture_workers < 1 or args.fsq_workers < 1 or args.match_workers < 1:
        raise SystemExit("worker counts must be at least 1")
    if args.source_tile_deg <= 0 or args.match_tile_deg <= 0:
        raise SystemExit("tile sizes must be positive")
    if args.max_distance <= 0:
        raise SystemExit("--max-distance must be positive")
    ratio = args.source_tile_deg / args.match_tile_deg
    if abs(ratio - round(ratio)) > 1e-9:
        raise SystemExit("--source-tile-deg must be an integer multiple of --match-tile-deg")

    root = Path(__file__).resolve().parents[2]
    config = load_area_config(root / "config" / "areas.yml")
    scope = resolve_scope(config, args.scope, args.areas)
    source_tiles = source_tiles_for_scope(scope, args.source_tile_deg)

    temp_dir = Path(args.temp_dir).expanduser().resolve() if args.temp_dir else root / "data" / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    # If a pinned Overture snapshot and its Philippine boundary are already
    # cached, use that mask even for --status / --only match / --only finalize.
    # This keeps restarts from expanding back to hundreds of ocean-only cells.
    ov_cache = root / "data" / "cache" / "overture"
    ov_marker = ov_cache / "release.json"
    if ov_marker.exists():
        try:
            pinned_release = json.loads(ov_marker.read_text(encoding="utf-8"))["release"]
            cached_boundary = ov_cache / f"ph_country_boundary_{pinned_release}.parquet"
            if valid_parquet(cached_boundary):
                source_tiles = filter_tiles_to_boundary(
                    source_tiles, cached_boundary, temp_dir, args.main_memory
                )
        except Exception:
            pass

    print(f"Scope: {scope.name}")
    print(f"Source blocks: {len(source_tiles)} at {args.source_tile_deg:g}°")
    print(
        f"Workers: Overture={args.overture_workers} | "
        f"Foursquare={args.fsq_workers} | match={args.match_workers}"
    )
    match_cfg = MatchConfig(max_distance_m=args.max_distance)
    print(
        f"Matching: max distance {match_cfg.max_distance_m:g} m | "
        f"blocking cell {match_cfg.grid_degrees:.6f}deg"
    )
    if args.max_distance > 120.0:
        print(
            "NOTE: the name-score ladder was calibrated at 120 m. Above that, "
            "the widest band still requires a >= 0.94 name score, but the "
            "false-match rate has not been validated."
        )
    print(f"DuckDB caps: worker={args.worker_memory}, main={args.main_memory}")
    print(f"DuckDB spill directory: {temp_dir}")
    free = free_gb(temp_dir)
    print(f"Free space at spill location: {free:.1f} GiB")
    if free < 15:
        print("WARNING: <15 GiB free. A full Philippines run may exhaust temporary disk space.")

    if args.status:
        _status(root, scope, source_tiles, args.source_tile_deg, args.match_tile_deg)
        return

    fsq_dir, overture_dir, osm_dir = _paths(root, scope.slug, args.source_tile_deg)

    # A refresh no longer forces rebuilds by itself: matching and finalization
    # compare the source snapshot recorded beside their checkpoints, which also
    # works when the stages run in separate sessions.
    rebuild_match = args.rebuild_match
    rebuild_finalize = args.rebuild_finalize or rebuild_match
    if args.refresh_sources and args.only in ("match", "finalize"):
        print("NOTE: --refresh-sources only affects the sources stage; it is ignored with "
              f"--only {args.only}.")

    try:
        if args.only in ("all", "sources"):
            check_external_tools()

            print("\n=== SOURCE 1/3: OpenStreetMap ===")
            osm_dir = prepare_osm(
                root, args.source_tile_deg, temp_dir, args.main_memory,
                refresh=args.refresh_sources,
                keep_intermediates=args.keep_intermediates,
            )

            print("\n=== SOURCE 2/3: Overture ===")
            overture_release = pin_overture_release(
                root, overture_dir, refresh=args.refresh_sources,
            )
            # The boundary file is named after its release, so a new release
            # gets a new boundary without deleting anything.
            boundary = prepare_overture_boundary(
                root, temp_dir, args.main_memory, overture_release,
            )
            source_tiles = filter_tiles_to_boundary(
                source_tiles_for_scope(scope, args.source_tile_deg),
                boundary, temp_dir, args.main_memory,
            )
            print(f"Land-intersecting source blocks: {len(source_tiles)}")
            overture_dir = prepare_overture(
                root, scope, source_tiles, overture_release, boundary,
                args.overture_workers, temp_dir, args.worker_memory,
                keep_raw=args.keep_intermediates,
            )

            print("\n=== SOURCE 3/3: Foursquare OS Places ===")
            fsq_dir = prepare_foursquare(
                root, scope, source_tiles,
                args.fsq_workers, temp_dir, args.worker_memory,
                refresh=args.refresh_sources,
            )

            if args.only == "sources":
                print("\nSource acquisition complete. You may shut down now and later run --only match.")
                return

        # Completeness and snapshot consistency are checked inside
        # prepare_matches() and finalize(), per source tile.
        edge_root = root / "data" / "work" / scope.slug / "edges"

        if args.only in ("all", "match"):
            print("\n=== TRIANGULATION / MATCHING ===")
            edge_root = prepare_matches(
                root, scope, source_tiles,
                args.source_tile_deg, args.match_tile_deg,
                fsq_dir, overture_dir, osm_dir,
                args.match_workers, temp_dir, args.worker_memory,
                match_cfg, rebuild=rebuild_match,
            )
            if args.only == "match":
                print("\nMatching complete. You may shut down now and later run --only finalize.")
                return

        if args.only in ("all", "finalize"):
            print("\n=== FINALIZE CANONICAL POINT LAYER ===")
            summary = finalize(
                root, scope, source_tiles,
                fsq_dir, overture_dir, osm_dir,
                edge_root, temp_dir, args.main_memory,
                rebuild=rebuild_finalize,
            )
            print("\nDone.")
            print(json.dumps(summary, indent=2))
            print(f"\nMain output: {root / 'data' / 'output' / scope.slug / 'canonical_pois.parquet'}")

    except KeyboardInterrupt:
        print("\nInterrupted safely. Completed checkpoints remain valid; rerun the same command to continue.")
        raise SystemExit(130)
    except PipelineStateError as exc:
        # An expected state problem with an instruction, not a program error.
        raise SystemExit(f"\nERROR: {exc}")


if __name__ == "__main__":
    main()
