"""Human-facing CLI for OpenPlaces PH 0.3.4.

Commands are task-oriented:

    openplaces build <area>
    openplaces status <area>
    openplaces validate <area>
    openplaces attribution <area>
    openplaces storage
    openplaces clean <area>

A bare ``openplaces`` prints help and never starts a national build.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Scope, load_area_config, resolve_scope
from .layout import areas_config_path, checkout_root, describe_root, project_root
from .licensing import licence_block
from .maintenance import (
    CleanupRefused,
    cleanup_targets,
    execute_cleanup,
    infer_output_sources,
    output_license_values,
    output_releases,
    print_cleanup_plan,
    print_storage,
    validate_output,
    write_attribution,
)
from .source_set import (
    ALL_SOURCES,
    DEFAULT_SOURCES,
    SourceSelectionError,
    display_sources,
    normalize_sources,
    pair_dir_name,
    source_pairs,
)

RUN_MANIFEST_NAME = "run.json"


def package_version() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:
        return "unknown"


def repo_root() -> Path:
    """Working root. Honours ``OPENPLACES_ROOT``; otherwise the checkout."""
    return project_root()


def code_identity() -> dict:
    """Best-effort Git identity of the code that is actually installed.

    A Git repository merely *above* an installed package is not sufficient:
    that could record an unrelated commit as provenance. We first require this
    module itself to be tracked by the candidate checkout. Wheel/non-Git
    installs return null values rather than making a build fail.
    """
    root = checkout_root()
    package_file = Path(__file__).resolve()
    try:
        relative = package_file.relative_to(root).as_posix()
        subprocess.check_output(
            ["git", "ls-files", "--error-unmatch", relative],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=root,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
        )
        return {"git_commit": commit, "git_dirty": dirty}
    except Exception:
        return {"git_commit": None, "git_dirty": None}


def _osm_cache_ready(root: Path, source_tile_deg: float) -> bool:
    _fsq, _overture, osm = _paths(root, "_unused", source_tile_deg)
    return (osm / "_SUCCESS.json").exists()


def _print_osm_first_run_notice(root: Path, source_tile_deg: float) -> None:
    if _osm_cache_ready(root, source_tile_deg):
        return
    print(
        "NOTE: OpenStreetMap preparation is national even for a local target.\n"
        "      The first OSM run downloads the Philippines Geofabrik extract and\n"
        "      builds a reusable national OSM tile cache. Temporary disk use can\n"
        "      be several times larger than the final local output.\n"
        "      For the lightest first run, use: --sources overture",
        flush=True,
    )


def _target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("areas", nargs="*", help="Area key(s) from config/areas.yml.")
    parser.add_argument(
        "--philippines", action="store_true", help="Explicitly use the national scope."
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("WEST", "SOUTH", "EAST", "NORTH"),
        help="Custom bounding box for a place not listed in areas.yml.",
    )
    parser.add_argument(
        "--name", help="Stable name for --bbox output (required with --bbox)."
    )


def _source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sources",
        nargs="+",
        help=(
            "Source set. Names: overture, osm/openstreetmap, fsq/foursquare. "
            "Default: overture osm."
        ),
    )
    parser.add_argument(
        "--with-foursquare",
        action="store_true",
        help="Add Foursquare OS Places to the default/explicit source set.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openplaces",
        description=(
            "Build a provenance-aware Philippine places layer. "
            "No target is assumed: national runs must be explicit."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"openplaces {package_version()}"
    )
    sub = parser.add_subparsers(dest="command")

    build = sub.add_parser("build", help="Build or resume a target.")
    _target_args(build)
    _source_args(build)
    build.add_argument(
        "--stage",
        choices=("all", "sources", "match", "finalize"),
        default="all",
        help="Advanced recovery control. Normally leave as all.",
    )
    build.add_argument("--refresh-sources", action="store_true")
    build.add_argument("--rebuild-match", action="store_true")
    build.add_argument("--rebuild-finalize", action="store_true")
    build.add_argument("--overture-workers", type=int, default=1)
    build.add_argument("--fsq-workers", type=int, default=2)
    build.add_argument("--match-workers", type=int, default=1)
    build.add_argument("--worker-memory", default="512MB")
    build.add_argument("--main-memory", default="1GB")
    build.add_argument("--temp-dir")
    build.add_argument("--source-tile-deg", type=float, default=1.0)
    build.add_argument("--match-tile-deg", type=float, default=0.25)
    build.add_argument("--max-distance", type=float, default=120.0)
    build.add_argument("--keep-intermediates", action="store_true")

    status = sub.add_parser("status", help="Show durable progress for a target.")
    _target_args(status)
    _source_args(status)
    status.add_argument("--source-tile-deg", type=float, default=1.0)
    status.add_argument("--match-tile-deg", type=float, default=0.25)
    status.add_argument("--temp-dir")

    validate = sub.add_parser(
        "validate", help="Run structural integrity checks on a completed target."
    )
    _target_args(validate)
    validate.add_argument(
        "--strict", action="store_true", help="Treat warnings as failures."
    )
    validate.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit a JSON report."
    )

    attribution = sub.add_parser(
        "attribution",
        help="Write ATTRIBUTION.txt beside a completed target's output.",
    )
    _target_args(attribution)

    sub.add_parser("storage", help="Show where OpenPlaces is using disk space.")

    clean = sub.add_parser(
        "clean", help="Preview or remove disposable data. Preview-only without --yes."
    )
    _target_args(clean)
    clean.add_argument(
        "--source-tiles",
        action="store_true",
        dest="remove_sources",
        help="Also remove normalized Overture/Foursquare tiles for this target.",
    )
    clean.add_argument(
        "--output", action="store_true", help="Also remove the final output for this target."
    )
    clean.add_argument(
        "--all",
        action="store_true",
        help="For the target, remove work, normalized source tiles, and output.",
    )
    clean.add_argument(
        "--raw-downloads",
        action="store_true",
        help="Remove raw OSM download/intermediates; keep reusable OSM tiles.",
    )
    clean.add_argument(
        "--hf-cache",
        action="store_true",
        help="Remove the external Hugging Face cache for Foursquare.",
    )
    clean.add_argument(
        "--shared-osm-cache",
        action="store_true",
        help="Also remove reusable national OSM tiles (expensive to rebuild).",
    )
    clean.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag, only print the cleanup plan.",
    )

    areas = sub.add_parser("areas", help="List configured area keys.")
    areas.add_argument("--search", help="Case-insensitive substring filter.")

    doctor = sub.add_parser("doctor", help="Check installation and source access.")
    _source_args(doctor)

    return parser


def _known_areas(root: Path) -> tuple[dict, list[str]]:
    path = areas_config_path(root)
    if not path.exists():
        raise SystemExit(
            f"No area configuration at {path}. "
            "Set OPENPLACES_ROOT to the checkout, or run from the checkout."
        )
    config = load_area_config(path)
    return config, sorted(config.get("areas") or {})


def resolve_target(args, root: Path, *, optional: bool = False) -> Scope | None:
    config, known = _known_areas(root)
    areas = tuple(getattr(args, "areas", ()) or ())
    philippines = bool(getattr(args, "philippines", False))
    bbox = getattr(args, "bbox", None)
    name = getattr(args, "name", None)

    modes = int(bool(areas)) + int(philippines) + int(bbox is not None)
    if modes == 0:
        if optional:
            return None
        raise SystemExit(
            "Choose one or more configured areas, --bbox ... --name ..., "
            "or --philippines. OpenPlaces does not assume a national run.\n"
            "List configured areas with: openplaces areas"
        )
    if modes > 1:
        raise SystemExit(
            "Choose exactly one target mode: area key(s), --bbox, or --philippines."
        )

    if philippines:
        return resolve_scope(config, "philippines", None)

    if areas:
        unknown = [a for a in areas if a not in known]
        if unknown:
            lines = [f"Unknown area key(s): {', '.join(unknown)}"]
            for item in unknown:
                near = difflib.get_close_matches(item, known, n=3, cutoff=0.5)
                if near:
                    lines.append(f"  {item}: did you mean {', '.join(near)}?")
            lines.append("List all configured areas with: openplaces areas")
            raise SystemExit("\n".join(lines))
        return resolve_scope(config, "philippines", areas)

    assert bbox is not None
    if not name:
        raise SystemExit("--name is required with --bbox.")
    west, south, east, north = map(float, bbox)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise SystemExit("Invalid --bbox. Expected WEST < EAST and SOUTH < NORTH.")
    clean_name = "".join(
        c if c.isalnum() or c in ("_", "-") else "_" for c in name.strip()
    ).strip("_")
    if not clean_name:
        raise SystemExit("--name must contain at least one letter or digit.")
    return Scope(
        name=f"bbox_{clean_name}",
        bboxes=((west, south, east, north),),
        full_philippines=False,
    )


def selected_sources(args, *, fallback: tuple[str, ...] | None = None) -> tuple[str, ...]:
    raw = getattr(args, "sources", None)
    default = fallback if fallback is not None else DEFAULT_SOURCES
    try:
        sources = list(normalize_sources(raw, default=default))
        if getattr(args, "with_foursquare", False) and "fsq" not in sources:
            sources.append("fsq")
        return normalize_sources(sources, default=default)
    except SourceSelectionError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


def _paths(root: Path, scope_slug: str, source_tile_deg: float):
    tag = str(source_tile_deg).replace(".", "p")
    return (
        root / "data" / "sources" / scope_slug / "fsq",
        root / "data" / "sources" / scope_slug / "overture",
        root / "data" / "cache" / "osm" / f"tiles_{tag}deg",
    )


def _cached_overture_boundary(root: Path):
    from .snapshot import pinned_release
    from .util import valid_parquet

    release = pinned_release(root, "overture")
    if not release:
        return None, None
    path = root / "data" / "cache" / "overture" / f"ph_country_boundary_{release}.parquet"
    return (path, release) if valid_parquet(path) else (None, None)


def _mask_tiles_if_possible(source_tiles, root, temp_dir, memory_limit):
    """Apply the cached Overture land mask when one exists.

    Returns the tiles and the boundary release used, or ``None`` when no mask
    was applied. The caller records that in the run manifest, because an
    unmasked run covers more tiles than a masked one for the same command.
    """
    boundary, release = _cached_overture_boundary(root)
    if boundary is None:
        return source_tiles, None
    from .sources import filter_tiles_to_boundary

    return filter_tiles_to_boundary(source_tiles, boundary, temp_dir, memory_limit), release


def _write_run_manifest(
    root: Path,
    scope: Scope,
    sources: tuple[str, ...],
    *,
    source_tile_count: int,
    boundary_release,
    args,
    summary,
    releases: dict,
    observed_licenses: dict[str, list[str]],
) -> Path:
    """Record what produced this output, next to the output.

    The source snapshot already records upstream releases. This adds the code
    version, the effective configuration, and whether a land mask was applied,
    so a result can be traced to an exact code plus config plus release triple.
    """
    out = root / "data" / "output" / scope.slug
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "openplaces_version": package_version(),
        **code_identity(),
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "scope": {
            "name": scope.name,
            "slug": scope.slug,
            "full_philippines": bool(scope.full_philippines),
            "bboxes": [list(b) for b in scope.bboxes],
        },
        "active_sources": list(sources),
        "pairs": [pair_dir_name(a, b) for a, b in source_pairs(sources)],
        "source_tile_count": source_tile_count,
        "source_tile_deg": args.source_tile_deg,
        "match_tile_deg": args.match_tile_deg,
        "max_distance_m": args.max_distance,
        "land_mask_boundary_release": boundary_release,
        "licensing": licence_block(
            sources,
            releases,
            observed_licenses=observed_licenses,
        ),
        "summary": summary,
    }
    path = out / RUN_MANIFEST_NAME
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _run_build(args) -> int:
    root = repo_root()
    scope = resolve_target(args, root)
    assert scope is not None
    sources = selected_sources(args)

    if args.overture_workers < 1 or args.fsq_workers < 1 or args.match_workers < 1:
        raise SystemExit("Worker counts must be at least 1.")
    if args.source_tile_deg <= 0 or args.match_tile_deg <= 0:
        raise SystemExit("Tile sizes must be positive.")
    if args.max_distance <= 0:
        raise SystemExit("--max-distance must be positive.")
    ratio = args.source_tile_deg / args.match_tile_deg
    if abs(ratio - round(ratio)) > 1e-9:
        raise SystemExit(
            "--source-tile-deg must be an integer multiple of --match-tile-deg."
        )

    from .finalize import finalize
    from .matching import MatchConfig, prepare_matches
    from .snapshot import PipelineStateError
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
    from .util import free_gb

    temp_dir = (
        Path(args.temp_dir).expanduser().resolve()
        if args.temp_dir
        else root / "data" / "tmp"
    )
    temp_dir.mkdir(parents=True, exist_ok=True)

    source_tiles, boundary_release = _mask_tiles_if_possible(
        source_tiles_for_scope(scope, args.source_tile_deg),
        root,
        temp_dir,
        args.main_memory,
    )

    cfg = MatchConfig(max_distance_m=args.max_distance)
    print(f"Root: {describe_root()}")
    print(f"Target: {scope.name}")
    print(f"Sources: {display_sources(sources)}")
    print(f"Source blocks: {len(source_tiles)} at {args.source_tile_deg:g}°")
    print(f"Matching pairs: {len(source_pairs(sources))}")
    if "overture" not in sources and boundary_release is None:
        print(
            "NOTE: no Overture land mask is available for this source set, so "
            "tiles are not clipped to land. Tile counts will differ from a run "
            "that includes Overture."
        )
    print(f"Temporary files: {temp_dir}")
    free = free_gb(temp_dir)
    print(f"Free space: {free:.1f} GiB")
    if free < 2:
        print("WARNING: less than 2 GiB free; even a local build may fail.")
    if scope.full_philippines and free < 15:
        print("WARNING: a Philippines-wide build should have at least ~15 GiB free.")
    if (
        "osm" in sources
        and not _osm_cache_ready(root, args.source_tile_deg)
        and free < 8
    ):
        print(
            "WARNING: first-time OSM preparation with less than 8 GiB free may be tight. "
            "Consider `openplaces storage`, cleanup, or `--sources overture`."
        )

    fsq_dir, overture_dir, osm_dir = _paths(root, scope.slug, args.source_tile_deg)
    rebuild_match = args.rebuild_match
    rebuild_finalize = args.rebuild_finalize or rebuild_match

    try:
        if args.stage in ("all", "sources"):
            if "osm" in sources:
                print("\n=== OpenStreetMap ===")
                _print_osm_first_run_notice(root, args.source_tile_deg)
                check_external_tools()
                osm_dir = prepare_osm(
                    root,
                    args.source_tile_deg,
                    temp_dir,
                    args.main_memory,
                    refresh=args.refresh_sources,
                    keep_intermediates=args.keep_intermediates,
                )

            if "overture" in sources:
                print("\n=== Overture Maps ===")
                release = pin_overture_release(
                    root, overture_dir, refresh=args.refresh_sources
                )
                boundary = prepare_overture_boundary(
                    root, temp_dir, args.main_memory, release
                )
                source_tiles = filter_tiles_to_boundary(
                    source_tiles_for_scope(scope, args.source_tile_deg),
                    boundary,
                    temp_dir,
                    args.main_memory,
                )
                boundary_release = release
                print(f"Land-intersecting source blocks: {len(source_tiles)}")
                overture_dir = prepare_overture(
                    root,
                    scope,
                    source_tiles,
                    release,
                    boundary,
                    args.overture_workers,
                    temp_dir,
                    args.worker_memory,
                    keep_raw=args.keep_intermediates,
                )
            else:
                source_tiles, boundary_release = _mask_tiles_if_possible(
                    source_tiles_for_scope(scope, args.source_tile_deg),
                    root,
                    temp_dir,
                    args.main_memory,
                )

            if "fsq" in sources:
                print("\n=== Foursquare OS Places (optional) ===")
                try:
                    fsq_dir = prepare_foursquare(
                        root,
                        scope,
                        source_tiles,
                        args.fsq_workers,
                        temp_dir,
                        args.worker_memory,
                        refresh=args.refresh_sources,
                    )
                except RuntimeError as exc:
                    raise SystemExit(str(exc)) from exc

            if args.stage == "sources":
                print("\nSource acquisition complete.")
                return 0

        edge_root = root / "data" / "work" / scope.slug / "edges"

        if args.stage in ("all", "match"):
            print("\n=== Matching ===")
            edge_root = prepare_matches(
                root,
                scope,
                source_tiles,
                args.source_tile_deg,
                args.match_tile_deg,
                fsq_dir,
                overture_dir,
                osm_dir,
                args.match_workers,
                temp_dir,
                args.worker_memory,
                cfg,
                sources=sources,
                rebuild=rebuild_match,
            )
            if args.stage == "match":
                print("\nMatching complete.")
                return 0

        if args.stage in ("all", "finalize"):
            print("\n=== Finalizing ===")
            summary = finalize(
                root,
                scope,
                source_tiles,
                fsq_dir,
                overture_dir,
                osm_dir,
                edge_root,
                temp_dir,
                args.main_memory,
                sources=sources,
                rebuild=rebuild_finalize,
            )
            print("\nDone.")
            print(json.dumps(summary, indent=2, default=str))

            releases = output_releases(root, scope.slug)
            observed_licenses = output_license_values(root, scope.slug)
            manifest = _write_run_manifest(
                root,
                scope,
                sources,
                source_tile_count=len(source_tiles),
                boundary_release=boundary_release,
                args=args,
                summary=summary,
                releases=releases,
                observed_licenses=observed_licenses,
            )
            attribution = write_attribution(
                root,
                scope,
                sources,
                releases=releases,
                observed_licenses=observed_licenses,
            )

            output = root / "data" / "output" / scope.slug / "canonical_pois.parquet"
            print(f"\nOutput:      {output}")
            print(f"Run record:  {manifest}")
            print(f"Attribution: {attribution}")
            print("Run `openplaces validate ...` on the same target to check it.")
            return 0

    except KeyboardInterrupt:
        print("\nInterrupted safely. Re-run the same command to continue.")
        return 130
    except PipelineStateError as exc:
        raise SystemExit(f"\nERROR: {exc}") from exc

    return 0


def _run_status(args) -> int:
    root = repo_root()
    scope = resolve_target(args, root)
    assert scope is not None

    inferred = infer_output_sources(root, scope.slug)
    sources = selected_sources(args, fallback=inferred or DEFAULT_SOURCES)

    from .matching import match_checkpoints_complete
    from .snapshot import dir_release, pinned_release
    from .sources import source_tiles_for_scope
    from .tiles import child_tiles, intersects
    from .util import read_json, valid_parquet

    temp_dir = (
        Path(args.temp_dir).expanduser().resolve()
        if args.temp_dir
        else root / "data" / "tmp"
    )
    temp_dir.mkdir(parents=True, exist_ok=True)

    source_tiles, boundary_release = _mask_tiles_if_possible(
        source_tiles_for_scope(scope, args.source_tile_deg), root, temp_dir, "1GB"
    )
    fsq_dir, overture_dir, osm_dir = _paths(root, scope.slug, args.source_tile_deg)

    print(f"Root: {describe_root()}")
    print(f"Target: {scope.name}")
    print(f"Sources: {display_sources(sources)}")
    print(f"Land mask: {boundary_release or 'none applied'}")

    if "osm" in sources:
        print(
            "OSM cache: "
            + ("ready" if (osm_dir / "_SUCCESS.json").exists() else "not ready")
        )

    for source, directory in (("overture", overture_dir), ("fsq", fsq_dir)):
        if source not in sources:
            continue
        pinned = pinned_release(root, source)
        bound = dir_release(directory)
        count = sum(
            valid_parquet(directory / f"{tile.key}.parquet") for tile in source_tiles
        )
        release = (
            "no release pinned"
            if pinned is None
            else f"pinned {pinned}"
            if bound in (None, pinned)
            else f"pinned {pinned}, tiles from {bound}"
        )
        print(f"{source}: {count}/{len(source_tiles)} blocks ({release})")

    expected_tiles = 0
    for block in source_tiles:
        for child in child_tiles(block, args.match_tile_deg):
            if any(intersects(child.bbox, bbox) for bbox in scope.bboxes):
                expected_tiles += 1
    pairs = source_pairs(sources)
    expected_edges = expected_tiles * len(pairs)

    edge_root = root / "data" / "work" / scope.slug / "edges"
    actual_edges = 0
    for a, b in pairs:
        pair_dir = edge_root / pair_dir_name(a, b)
        if pair_dir.exists():
            actual_edges += sum(
                1 for path in pair_dir.glob("*.parquet") if valid_parquet(path)
            )

    manifest = read_json(edge_root / "_config.json") or {}
    recorded_raw = manifest.get("active_sources")
    recorded = (
        tuple(recorded_raw)
        if recorded_raw
        else ALL_SOURCES
        if manifest.get("sources")
        else ()
    )
    same_set = recorded == tuple(sources)
    recorded_tiles = manifest.get("source_tiles")
    expected_source_tiles = [tile.key for tile in source_tiles]
    same_tiles = recorded_tiles == expected_source_tiles
    modern_manifest = bool(recorded_raw) and isinstance(recorded_tiles, list)
    complete = (
        match_checkpoints_complete(edge_root)
        and same_set
        and same_tiles
        and modern_manifest
    )
    if complete:
        suffix = " (complete)"
    elif manifest and not modern_manifest:
        suffix = " (legacy checkpoint config; rebuild on next match)"
    elif recorded and not same_set:
        suffix = " (different source set)"
    elif recorded_tiles is not None and not same_tiles:
        suffix = " (different source-tile inventory)"
    else:
        suffix = ""
    print(f"Match checkpoints: {actual_edges}/{expected_edges}{suffix}")

    out = root / "data" / "output" / scope.slug
    for name in (
        "observations.parquet",
        "match_edges.parquet",
        "canonical_pois.parquet",
        "summary.json",
        RUN_MANIFEST_NAME,
        "ATTRIBUTION.txt",
    ):
        print(f"{name}: {'ready' if (out / name).exists() else 'not ready'}")
    return 0


def _run_clean(args) -> int:
    root = repo_root()
    scope = resolve_target(args, root, optional=True)
    slug = scope.slug if scope else None

    remove_sources = args.remove_sources or args.all
    remove_output = args.output or args.all
    try:
        targets = cleanup_targets(
            root,
            slug,
            remove_sources=remove_sources,
            remove_output=remove_output,
            remove_raw_downloads=args.raw_downloads,
            remove_hf_cache=args.hf_cache,
            remove_shared_osm_cache=args.shared_osm_cache,
        )
        print_cleanup_plan(targets)
        if not args.yes:
            print("\nPreview only. Add --yes to delete these paths.")
            return 0
        execute_cleanup(targets, root)
    except CleanupRefused as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    return 0


def _run_areas(args) -> int:
    root = repo_root()
    _config, names = _known_areas(root)
    if args.search:
        needle = args.search.casefold()
        names = [name for name in names if needle in name.casefold()]
    for name in names:
        print(name)
    if not names:
        print("(no matching areas)")
    return 0


def _run_doctor(args) -> int:
    sources = selected_sources(args)
    ok = True
    print(f"OpenPlaces: {package_version()}")
    print(f"Root: {describe_root()}")
    print(f"Sources: {display_sources(sources)}")

    config = areas_config_path()
    print(f"Area config: {'found' if config.exists() else 'MISSING'} ({config})")
    ok &= config.exists()

    try:
        import duckdb

        print(f"DuckDB: {duckdb.__version__}")
    except Exception as exc:
        print(f"DuckDB: FAIL ({exc})")
        ok = False

    try:
        import pyarrow

        print(f"PyArrow: {pyarrow.__version__}")
    except Exception as exc:
        print(f"PyArrow: FAIL ({exc})")
        ok = False

    if "osm" in sources:
        osmium = shutil.which("osmium")
        print(f"Osmium: {osmium or 'NOT FOUND'}")
        ok &= osmium is not None

    if "overture" in sources:
        try:
            import overturemaps  # noqa: F401

            print("Overture client: available")
        except Exception as exc:
            print(f"Overture client: FAIL ({exc})")
            ok = False

    if "fsq" in sources:
        try:
            from .sources import _discover_fsq_release

            release = _discover_fsq_release()
            print(f"Foursquare/Hugging Face: access OK (latest {release})")
        except Exception as exc:
            print(f"Foursquare/Hugging Face: FAIL ({exc})")
            ok = False

    from .util import free_gb

    temp = repo_root() / "data" / "tmp"
    temp.mkdir(parents=True, exist_ok=True)
    print(f"Free disk at project temp: {free_gb(temp):.1f} GiB")
    print("PASS" if ok else "CHECK ITEMS ABOVE")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> None:
    argsv = list(sys.argv[1:] if argv is None else argv)

    parser = build_parser()
    if not argsv:
        parser.print_help()
        return

    args = parser.parse_args(argsv)

    if args.command == "build":
        code = _run_build(args)
    elif args.command == "status":
        code = _run_status(args)
    elif args.command == "validate":
        root = repo_root()
        scope = resolve_target(args, root)
        assert scope is not None
        code = 0 if validate_output(
            root,
            scope,
            sources=infer_output_sources(root, scope.slug),
            strict=args.strict,
            as_json=args.as_json,
        ) else 1
    elif args.command == "attribution":
        root = repo_root()
        scope = resolve_target(args, root)
        assert scope is not None
        sources = infer_output_sources(root, scope.slug)
        if not sources:
            raise SystemExit(
                "Cannot determine the source set for this target. "
                "Build it first, or pass --sources on a future run."
            )
        print(write_attribution(root, scope, sources))
        code = 0
    elif args.command == "storage":
        print_storage(repo_root())
        code = 0
    elif args.command == "clean":
        code = _run_clean(args)
    elif args.command == "areas":
        code = _run_areas(args)
    elif args.command == "doctor":
        code = _run_doctor(args)
    else:
        parser.print_help()
        code = 0

    raise SystemExit(code)


if __name__ == "__main__":
    main()
