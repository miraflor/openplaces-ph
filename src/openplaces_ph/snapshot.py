"""Identity of the source vintages that downstream checkpoints depend on.

Why this module exists
----------------------
Every durable checkpoint after acquisition (match shards, observations,
clusters, the canonical layer) is valid only for the exact source vintages it
was computed from. Up to 0.2.0 that dependency travelled only through the
``--refresh-sources`` flag *inside one invocation*:

* ``--only sources --refresh-sources`` in one session, then ``--only match``
  and ``--only finalize`` in later sessions, silently reused match shards and
  final outputs computed from the previous snapshot;
* an interrupted refresh left a source directory holding tiles from two
  releases, and a resume without the flag accepted them as complete;
* ``--only match`` after an interrupted acquisition wrote *durable* empty edge
  shards for tiles that had simply not been downloaded yet.

The rules implemented here:

1. A normalized FSQ/Overture directory is bound to exactly one release by
   ``_release.json``. When the pinned release changes, the whole directory is
   discarded *before* any new tile is written.
2. The OSM tile directory records the identity of the PBF it was built from
   in its ``_SUCCESS.json``.
3. :func:`source_snapshot` verifies that the three layers are complete for the
   requested tiles and returns one dictionary describing them. Matching and
   finalization store it beside their checkpoints and rebuild, or refuse to
   continue, when it changes.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Iterable

from .tiles import Tile
from .util import atomic_json, read_json, valid_parquet
from .source_set import ALL_SOURCES, normalize_sources

RELEASE_MANIFEST = "_release.json"


class PipelineStateError(RuntimeError):
    """Durable state is incomplete or inconsistent; the message says what to run.

    A subclass of RuntimeError, so existing callers that catch RuntimeError
    keep working; the CLI prints it without a Python traceback.
    """

_DISCARD_MARK = ".discard-"

# Where acquisition pins each release (written by resolve_*_release).
_PIN_FOLDER = {"fsq": "foursquare", "overture": "overture"}


def pinned_release(root: Path, source: str) -> str | None:
    """Release pinned by the last acquisition run, read without network access."""
    marker = root / "data" / "cache" / _PIN_FOLDER[source] / "release.json"
    value = (read_json(marker) or {}).get("release")
    return str(value) if value else None


def dir_release(directory: Path) -> str | None:
    """Release that a normalized source directory is bound to, if recorded."""
    value = (read_json(directory / RELEASE_MANIFEST) or {}).get("release")
    return str(value) if value else None


def _sweep_discarded(path: Path) -> None:
    """Delete leftovers of an earlier :func:`discard_dir` that crashed mid-way."""
    if not path.parent.exists():
        return
    for leftover in path.parent.glob(f"{path.name}{_DISCARD_MARK}*"):
        shutil.rmtree(leftover, ignore_errors=True)


def discard_dir(path: Path) -> None:
    """Remove a directory without ever leaving a half-deleted one in place.

    ``shutil.rmtree`` deletes entries one by one. If the process dies in the
    middle, the directory still exists and may still *look* valid, for example
    a manifest already deleted while stale shards remain. Renaming first is
    atomic on the same filesystem, so the original path either still holds the
    complete old content or does not exist at all.
    """
    _sweep_discarded(path)
    if not path.exists():
        return
    trash = path.with_name(f"{path.name}{_DISCARD_MARK}{os.getpid()}-{time.time_ns()}")
    os.replace(path, trash)
    shutil.rmtree(trash, ignore_errors=True)


def adopt_unbound_dir(directory: Path, release: str | None, label: str) -> None:
    """Bind a directory written by <= 0.2.0 (no manifest) to the release pinned then.

    Must run *before* a refresh re-pins the release; otherwise old tiles would
    be labelled with the new release. Tiles from an interrupted 0.2.0 refresh
    cannot be detected after the fact, so the message says how to force a
    clean download.
    """
    if release is None or dir_release(directory) is not None:
        return
    if not directory.exists() or not any(directory.glob("*.parquet")):
        return
    atomic_json(directory / RELEASE_MANIFEST, {"release": release, "adopted": True})
    print(
        f"[{label}] existing tiles had no release manifest; recorded them as release {release}. "
        f"If an earlier --refresh-sources run was interrupted, delete {directory} "
        "to download that source again.",
        flush=True,
    )


def bind_dir_to_release(directory: Path, release: str, label: str) -> None:
    """Guarantee that ``directory`` only ever holds tiles of ``release``."""
    _sweep_discarded(directory)
    bound = dir_release(directory)
    if bound == release:
        return
    if bound is not None:
        print(f"[{label}] release changed {bound} -> {release}; discarding old normalized tiles", flush=True)
        discard_dir(directory)
    elif directory.exists() and any(directory.glob("*.parquet")):
        # Unbound tiles of unknown vintage. adopt_unbound_dir() runs before a
        # re-pin, so reaching this point means the vintage cannot be trusted.
        print(f"[{label}] tiles of unknown release found; discarding them", flush=True)
        discard_dir(directory)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / RELEASE_MANIFEST, {"release": release})


def source_snapshot(
    root: Path,
    source_tiles: Iterable[Tile],
    fsq_dir: Path,
    overture_dir: Path,
    osm_dir: Path,
    sources: tuple[str, ...] = ALL_SOURCES,
) -> dict:
    """Return the identity of complete, consistent selected source layers."""
    selected = normalize_sources(sources, default=ALL_SOURCES)
    active = set(selected)
    tiles = list(source_tiles)
    problems: list[str] = []
    # The active source set is part of the durable identity for every 0.3 run.
    # Old 0.2 summaries remain readable through infer_output_sources().
    snapshot: dict = {"active_sources": list(selected)}

    if "osm" in active:
        osm_success = read_json(osm_dir / "_SUCCESS.json")
        if osm_success is None:
            problems.append("OpenStreetMap tiles are not prepared")
        snapshot["osm"] = osm_success

    for source, directory in (("fsq", fsq_dir), ("overture", overture_dir)):
        if source not in active:
            continue

        pinned = pinned_release(root, source)
        adopt_unbound_dir(directory, pinned, source)
        bound = dir_release(directory)
        if pinned is None:
            problems.append(f"{source}: no pinned release (acquisition has not run)")
        elif bound != pinned:
            problems.append(
                f"{source}: normalized tiles belong to release {bound} but release {pinned} "
                "is pinned; source acquisition has not finished for this scope"
            )

        missing = [
            tile.key
            for tile in tiles
            if not valid_parquet(directory / f"{tile.key}.parquet")
        ]
        if missing:
            problems.append(
                f"{source}: {len(missing)} of {len(tiles)} source tiles are missing "
                f"(first: {missing[0]})"
            )

        snapshot[f"{source}_release"] = bound

    if problems:
        raise PipelineStateError(
            "Source checkpoints are incomplete or inconsistent:\n  - "
            + "\n  - ".join(problems)
            + "\nRun the source stage with the same target and source set."
        )
    return snapshot
