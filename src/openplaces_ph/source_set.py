"""Source selection for OpenPlaces PH.

Internal source names remain ``fsq``, ``overture``, and ``osm`` for backwards
compatibility with the existing Parquet schema. The CLI accepts friendlier
aliases such as ``foursquare`` and ``openstreetmap``.

This module is the single source of truth for three things:

1. which sources exist and in which order they are always written;
2. which cross-source pairs are matched for a given selection;
3. how an edge-shard directory is named for a pair.

``matching.py`` must use :data:`ALL_PAIRS` rather than defining its own
``PAIRS`` constant. If the two ever disagree, edge shards are written under
different directory names and existing checkpoints are silently orphaned.
``tests/test_migration_applied_v03.py`` asserts that they agree.
"""

from __future__ import annotations

from itertools import combinations
from typing import Iterable

ALL_SOURCES: tuple[str, ...] = ("fsq", "overture", "osm")
DEFAULT_SOURCES: tuple[str, ...] = ("overture", "osm")

ALIASES: dict[str, str] = {
    "fsq": "fsq",
    "foursquare": "fsq",
    "4sq": "fsq",
    "overture": "overture",
    "overturemaps": "overture",
    "osm": "osm",
    "openstreetmap": "osm",
    "open-street-map": "osm",
}

DISPLAY_NAME: dict[str, str] = {
    "fsq": "Foursquare OS Places",
    "overture": "Overture Maps Places",
    "osm": "OpenStreetMap",
}


class SourceSelectionError(ValueError):
    """A requested source set cannot be resolved.

    This subclasses ``ValueError`` so existing ``except ValueError`` handlers
    keep working. The CLI catches it specifically and converts it into a
    clean message instead of a traceback.
    """


def normalize_sources(
    values: Iterable[str] | None,
    *,
    default: tuple[str, ...] = DEFAULT_SOURCES,
) -> tuple[str, ...]:
    """Return a validated source tuple in stable internal order.

    The returned order always follows :data:`ALL_SOURCES`, whatever order the
    caller supplied, so the same selection always produces the same pair list,
    the same directory names, and the same checkpoint identity.
    """
    raw = tuple(default if values is None else values)
    if not raw:
        raise SourceSelectionError("At least one source must be selected.")

    chosen: set[str] = set()
    unknown: list[str] = []
    for value in raw:
        key = str(value).strip().lower()
        source = ALIASES.get(key)
        if source is None:
            unknown.append(str(value))
        else:
            chosen.add(source)

    if unknown:
        allowed = ", ".join(sorted(set(ALIASES)))
        raise SourceSelectionError(
            f"Unknown source(s): {', '.join(unknown)}. Allowed names: {allowed}"
        )

    return tuple(source for source in ALL_SOURCES if source in chosen)


def source_pairs(sources: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """All cross-source pairs for the selected source set.

    Pair members are ordered by :data:`ALL_SOURCES`, so ``("fsq", "osm")`` is
    produced and ``("osm", "fsq")`` never is.
    """
    normalized = normalize_sources(sources, default=ALL_SOURCES)
    return tuple(combinations(normalized, 2))


#: The full pair list. ``matching.PAIRS`` must be this exact tuple.
ALL_PAIRS: tuple[tuple[str, str], ...] = source_pairs(ALL_SOURCES)


def pair_dir_name(a: str, b: str) -> str:
    """Directory name for one pair's edge shards."""
    return f"{a}__{b}"


def display_sources(sources: Iterable[str]) -> str:
    normalized = normalize_sources(sources, default=ALL_SOURCES)
    return ", ".join(DISPLAY_NAME[source] for source in normalized)


def is_default(sources: Iterable[str]) -> bool:
    """True when the selection equals the zero-auth default set."""
    return normalize_sources(sources, default=ALL_SOURCES) == DEFAULT_SOURCES
