"""Disk inspection, guarded cleanup, and completed-output validation."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Scope, bbox_sql
from .layout import fsq_cache_dir
from .licensing import attribution_text
from .source_set import ALL_SOURCES, normalize_sources


class CleanupRefused(RuntimeError):
    """A cleanup target fell outside the paths this tool is allowed to remove."""


def size_bytes(path: Path) -> int:
    """Best-effort recursive size that never follows directory symlinks."""
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for filename in filenames:
            item = Path(dirpath) / filename
            if item.is_symlink():
                continue
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def human_bytes(value: int) -> str:
    n = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{value} B"


@dataclass(frozen=True)
class StorageItem:
    label: str
    path: Path

    @property
    def bytes(self) -> int:
        return size_bytes(self.path)


def storage_items(root: Path) -> list[StorageItem]:
    """Important project and external caches, intentionally not every file."""
    return [
        StorageItem("OSM reusable cache", root / "data" / "cache" / "osm"),
        StorageItem("Overture cache", root / "data" / "cache" / "overture"),
        StorageItem("Foursquare release cache", root / "data" / "cache" / "foursquare"),
        StorageItem("Normalized area sources", root / "data" / "sources"),
        StorageItem("Work/checkpoints", root / "data" / "work"),
        StorageItem("Final outputs", root / "data" / "output"),
        StorageItem("DuckDB temporary spill", root / "data" / "tmp"),
        StorageItem("Hugging Face FSQ cache", fsq_cache_dir()),
    ]


def print_storage(root: Path) -> None:
    rows = [(item.label, item.path, item.bytes) for item in storage_items(root)]
    width = max(len(label) for label, _, _ in rows)
    for label, path, amount in sorted(rows, key=lambda row: row[2], reverse=True):
        print(f"{label:<{width}}  {human_bytes(amount):>12}  {path}")
    print()
    print(f"Tracked total: {human_bytes(sum(amount for _, _, amount in rows))}")


@dataclass(frozen=True)
class CleanTarget:
    label: str
    path: Path


def _valid_slug(slug: str) -> bool:
    """Reject anything that could escape ``data/<kind>/`` when joined."""
    if not slug or slug in (".", ".."):
        return False
    if slug != slug.strip():
        return False
    return not any(sep in slug for sep in ("/", "\\", os.sep)) and ":" not in slug


def allowed_removal_roots(root: Path) -> tuple[Path, ...]:
    """The only trees cleanup may touch."""
    return ((root / "data").resolve(), fsq_cache_dir())


def assert_removable(path: Path, root: Path) -> None:
    resolved = path.resolve()
    for allowed in allowed_removal_roots(root):
        if resolved == allowed or allowed in resolved.parents:
            return
    raise CleanupRefused(
        f"Refusing to remove {resolved}: outside {root / 'data'} "
        "and outside the Hugging Face Foursquare cache."
    )


def cleanup_targets(
    root: Path,
    scope_slug: str | None,
    *,
    remove_sources: bool = False,
    remove_output: bool = False,
    remove_raw_downloads: bool = False,
    remove_hf_cache: bool = False,
    remove_shared_osm_cache: bool = False,
) -> list[CleanTarget]:
    """Return exactly what a cleanup command is allowed to remove.

    The expensive reusable OSM tile cache is never part of ordinary cleanup.
    It requires ``remove_shared_osm_cache=True`` explicitly.
    """
    if scope_slug is not None and not _valid_slug(scope_slug):
        raise CleanupRefused(f"Refusing to use {scope_slug!r} as a scope directory name.")

    targets: list[CleanTarget] = [
        CleanTarget("DuckDB temporary spill", root / "data" / "tmp"),
    ]

    if scope_slug:
        targets.append(
            CleanTarget("Scope work/checkpoints", root / "data" / "work" / scope_slug)
        )
        if remove_sources:
            targets.append(
                CleanTarget(
                    "Scope normalized sources",
                    root / "data" / "sources" / scope_slug,
                )
            )
        if remove_output:
            targets.append(
                CleanTarget("Scope output", root / "data" / "output" / scope_slug)
            )

    if remove_raw_downloads:
        targets.extend(
            [
                CleanTarget(
                    "Raw national OSM PBF",
                    root / "data" / "cache" / "osm" / "philippines-latest.osm.pbf",
                ),
                CleanTarget(
                    "OSM preparation intermediates", root / "data" / "work" / "osm"
                ),
            ]
        )

    if remove_hf_cache:
        targets.append(CleanTarget("Hugging Face FSQ cache", fsq_cache_dir()))

    if remove_shared_osm_cache:
        cache = root / "data" / "cache" / "osm"
        for path in sorted(cache.glob("tiles_*deg")) if cache.exists() else []:
            targets.append(CleanTarget("Reusable OSM tile cache", path))

    seen: set[Path] = set()
    result: list[CleanTarget] = []
    for target in targets:
        assert_removable(target.path, root)
        path = target.path.resolve()
        if path not in seen:
            result.append(CleanTarget(target.label, path))
            seen.add(path)
    return result


def print_cleanup_plan(targets: list[CleanTarget]) -> int:
    total = 0
    print("Cleanup plan:")
    for target in targets:
        amount = size_bytes(target.path)
        total += amount
        status = human_bytes(amount) if target.path.exists() else "not present"
        print(f"  {target.label}: {status}")
        print(f"    {target.path}")
    print(f"\nPotentially reclaimable: {human_bytes(total)}")
    return total


def execute_cleanup(targets: list[CleanTarget], root: Path) -> None:
    for target in targets:
        assert_removable(target.path, root)
        path = target.path
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            print(f"Skipped symlink: {path}")
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        print(f"Removed: {path}")


def infer_output_sources(root: Path, scope_slug: str) -> tuple[str, ...] | None:
    """Read source selection from a completed summary when available.

    Old 0.2 summaries did not have ``active_sources`` because all three sources
    were mandatory, so infer that historical case from the snapshot keys.
    """
    path = root / "data" / "output" / scope_slug / "summary.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources = payload.get("sources") or {}
        active = sources.get("active_sources")
        if isinstance(active, list) and active:
            return normalize_sources(active, default=ALL_SOURCES)

        inferred: list[str] = []
        if sources.get("fsq_release") is not None:
            inferred.append("fsq")
        if sources.get("overture_release") is not None:
            inferred.append("overture")
        if sources.get("osm") is not None:
            inferred.append("osm")
        return tuple(inferred) if inferred else None
    except Exception:
        return None


def output_releases(root: Path, scope_slug: str) -> dict:
    """Source releases recorded in a completed summary, if any."""
    path = root / "data" / "output" / scope_slug / "summary.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload.get("sources") or {})
    except Exception:
        return {}


def output_license_values(root: Path, scope_slug: str) -> dict[str, list[str]]:
    """Distinct source licence values actually present in a completed output.

    The canonical file is scanned once for all available licence columns. This
    is evidence for attribution, not a conclusion about the licence of the
    combined database. Missing/old outputs simply return an empty mapping.
    """
    path = root / "data" / "output" / scope_slug / "canonical_pois.parquet"
    if not path.exists():
        return {}
    try:
        import duckdb

        con = duckdb.connect()
        try:
            literal = _sql_literal(path.as_posix())
            columns = {
                row[0]
                for row in con.execute(
                    f"DESCRIBE SELECT * FROM read_parquet({literal})"
                ).fetchall()
            }
            available = [
                (source, column)
                for source, column in (
                    ("fsq", "fsq_license"),
                    ("overture", "overture_license"),
                    ("osm", "osm_license"),
                )
                if column in columns
            ]
            if not available:
                return {}

            expressions = [
                f"list_sort(list(DISTINCT {column}) FILTER (WHERE {column} IS NOT NULL))"
                for _source, column in available
            ]
            row = con.execute(
                "SELECT " + ", ".join(expressions) + f" FROM read_parquet({literal})"
            ).fetchone()
            return {
                source: [str(value) for value in (values or [])]
                for (source, _column), values in zip(available, row)
            }
        finally:
            con.close()
    except Exception:
        return {}


def write_attribution(
    root: Path,
    scope: Scope,
    sources,
    *,
    releases: dict | None = None,
    observed_licenses: dict[str, list[str]] | None = None,
) -> Path:
    """Write ``ATTRIBUTION.txt`` beside the canonical output.

    Callers that already inspected the canonical file can pass the observed
    values so finalization does not scan the same output twice.
    """
    out = root / "data" / "output" / scope.slug
    out.mkdir(parents=True, exist_ok=True)
    path = out / "ATTRIBUTION.txt"
    path.write_text(
        attribution_text(
            sources,
            releases=releases if releases is not None else output_releases(root, scope.slug),
            scope_name=scope.name,
            observed_licenses=(
                observed_licenses
                if observed_licenses is not None
                else output_license_values(root, scope.slug)
            ),
        ),
        encoding="utf-8",
    )
    return path


def _sql_literal(value: str) -> str:
    """Single-quoted SQL literal with embedded quotes doubled.

    Paths on Windows can contain an apostrophe, for example in a user folder
    named ``James O'Brien``. The 0.3 candidate interpolated the path directly
    and produced a syntax error there.
    """
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _read_run_manifest(root: Path, scope_slug: str) -> dict | None:
    path = root / "data" / "output" / scope_slug / "run.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def validate_output(
    root: Path,
    scope: Scope,
    *,
    sources: tuple[str, ...] | None = None,
    strict: bool = False,
    as_json: bool = False,
) -> bool:
    """Structural checks for a completed canonical layer; no pandas required.

    Failures are conditions that make the output unusable. Warnings are
    conditions worth looking at that do not by themselves invalidate a run.
    ``strict=True`` promotes warnings to failures.
    """
    try:
        import duckdb
    except ModuleNotFoundError as exc:
        raise RuntimeError("DuckDB is required for validation.") from exc

    path = root / "data" / "output" / scope.slug / "canonical_pois.parquet"
    if not path.exists():
        message = f"No canonical output found: {path}"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return False

    selected = normalize_sources(sources, default=ALL_SOURCES) if sources else None
    run_manifest = _read_run_manifest(root, scope.slug)
    run_sources = None
    run_dirty = None
    run_source_invalid = False
    if run_manifest is not None:
        raw_sources = run_manifest.get("active_sources")
        if isinstance(raw_sources, list) and raw_sources:
            try:
                run_sources = normalize_sources(raw_sources, default=ALL_SOURCES)
            except Exception:
                run_source_invalid = True
        else:
            run_source_invalid = True
        run_dirty = run_manifest.get("git_dirty")

    con = duckdb.connect()
    try:
        con.execute(
            "CREATE VIEW canonical AS "
            f"SELECT * FROM read_parquet({_sql_literal(path.as_posix())})"
        )

        columns = {row[0] for row in con.execute("DESCRIBE canonical").fetchall()}
        required = {
            "canonical_id",
            "canonical_name",
            "lon",
            "lat",
            "source_count",
            "evidence_tier",
        }
        missing_columns = sorted(required - columns)
        if missing_columns:
            message = "FAIL: missing columns: " + ", ".join(missing_columns)
            print(
                json.dumps({"ok": False, "missing_columns": missing_columns})
                if as_json
                else message
            )
            return False

        (
            rows,
            unique_ids,
            duplicate_ids,
            null_ids,
            missing_names,
            missing_coords,
            out_of_range,
            null_island,
            null_tier,
            min_sources,
            max_sources,
        ) = con.execute(
            """
            SELECT
                count(*),
                count(DISTINCT canonical_id),
                count(*) - count(DISTINCT canonical_id),
                count(*) FILTER (WHERE canonical_id IS NULL),
                count(*) FILTER (
                    WHERE canonical_name IS NULL OR trim(canonical_name) = ''
                ),
                count(*) FILTER (WHERE lon IS NULL OR lat IS NULL),
                count(*) FILTER (
                    WHERE lon < -180 OR lon > 180 OR lat < -90 OR lat > 90
                ),
                count(*) FILTER (WHERE lon = 0 AND lat = 0),
                count(*) FILTER (WHERE evidence_tier IS NULL),
                min(source_count),
                max(source_count)
            FROM canonical
            """
        ).fetchone()

        outside = con.execute(
            "SELECT count(*) FROM canonical "
            f"WHERE NOT {bbox_sql('lon', 'lat', scope.bboxes)}"
        ).fetchone()[0]

        membership_columns = [
            column for column in ("fsq_id", "overture_id", "osm_id") if column in columns
        ]
        source_mismatch = 0
        if membership_columns:
            expression = " + ".join(
                f"CASE WHEN {column} IS NOT NULL THEN 1 ELSE 0 END"
                for column in membership_columns
            )
            source_mismatch = con.execute(
                f"SELECT count(*) FROM canonical WHERE source_count <> ({expression})"
            ).fetchone()[0]

        near_duplicates = con.execute(
            """
            SELECT count(*) FROM (
                SELECT lower(trim(canonical_name)) AS n,
                       round(lon, 5) AS x,
                       round(lat, 5) AS y,
                       count(*) AS c
                FROM canonical
                WHERE canonical_name IS NOT NULL
                GROUP BY 1, 2, 3
                HAVING count(*) > 1
            )
            """
        ).fetchone()[0]

        tiers = con.execute(
            "SELECT evidence_tier, count(*) FROM canonical "
            "GROUP BY evidence_tier ORDER BY evidence_tier"
        ).fetchall()

        effective_sources = selected or run_sources
        expected_max = len(effective_sources) if effective_sources else len(ALL_SOURCES)
        too_many_sources = (max_sources or 0) > expected_max

        present_sources: set[str] = set()
        for source, column in (("fsq", "fsq_id"), ("overture", "overture_id"), ("osm", "osm_id")):
            if column not in columns:
                continue
            count = con.execute(
                f"SELECT count(*) FROM canonical WHERE {column} IS NOT NULL"
            ).fetchone()[0]
            if count:
                present_sources.add(source)

        expected_sources = effective_sources
        inactive_members = (
            sorted(present_sources - set(expected_sources))
            if expected_sources is not None
            else []
        )
        run_source_mismatch = bool(
            selected is not None
            and run_sources is not None
            and tuple(selected) != tuple(run_sources)
        )

        failures = {
            "duplicate_canonical_ids": duplicate_ids,
            "null_canonical_ids": null_ids,
            "missing_names": missing_names,
            "missing_coordinates": missing_coords,
            "coordinates_out_of_range": out_of_range,
            "outside_requested_scope": outside,
            "source_count_mismatches": source_mismatch,
            "source_count_below_one": 1 if rows > 0 and (min_sources or 0) < 1 else 0,
            "source_count_above_selection": 1 if too_many_sources else 0,
            "run_source_set_mismatch": 1 if run_source_mismatch else 0,
            "inactive_source_members_present": len(inactive_members),
        }
        warnings = {
            "null_island_coordinates": null_island,
            "null_evidence_tier": null_tier,
            "duplicate_name_and_location": near_duplicates,
            "run_manifest_missing": 1 if run_manifest is None else 0,
            "run_manifest_source_set_invalid": 1 if run_source_invalid else 0,
            "run_built_from_dirty_tree": 1 if run_dirty is True else 0,
        }

        failed = any(value for value in failures.values())
        warned = any(value for value in warnings.values())
        ok = not failed and not (strict and warned)

        report = {
            "ok": ok,
            "path": str(path),
            "rows": rows,
            "unique_canonical_ids": unique_ids,
            "source_count_range": [min_sources, max_sources],
            "expected_max_source_count": expected_max,
            "selected_sources": list(selected) if selected else None,
            "run_manifest_sources": list(run_sources) if run_sources else None,
            "present_sources": sorted(present_sources),
            "inactive_source_members": inactive_members,
            "git_dirty": run_dirty,
            "evidence_tiers": {str(tier): count for tier, count in tiers},
            "failures": failures,
            "warnings": warnings,
            "strict": strict,
        }

        if as_json:
            print(json.dumps(report, indent=2, default=str))
            return ok

        print(f"Rows:                     {rows}")
        print(f"Unique canonical IDs:     {unique_ids}")
        print(f"Source count range:       {min_sources}-{max_sources} "
              f"(selection allows up to {expected_max})")
        print(
            "Evidence tiers:           "
            + (", ".join(f"{tier}={count}" for tier, count in tiers) or "none")
        )
        print()
        print("Checks that must pass:")
        for name, value in failures.items():
            mark = "ok " if not value else "FAIL"
            print(f"  [{mark}] {name}: {value}")
        print()
        print("Checks that only warn:" + (" (strict: these fail)" if strict else ""))
        for name, value in warnings.items():
            mark = "ok " if not value else "warn"
            print(f"  [{mark}] {name}: {value}")
        print("\nPASS" if ok else "\nFAIL")
        return ok
    finally:
        con.close()
