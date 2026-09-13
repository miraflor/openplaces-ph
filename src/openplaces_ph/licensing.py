"""Upstream licence and attribution metadata.

This module records source-specific terms and observed licence values. It does
*not* decide the legal licence of a combined OpenPlaces PH output. In
particular, Overture Places is multi-license at the provider level and ODbL
obligations depend on the concrete way a derived database is used/distributed.
See DATA_LICENSES.md before publishing data.
"""

from __future__ import annotations

import datetime as _dt
from typing import Iterable, Mapping, Sequence

from .source_set import DISPLAY_NAME, normalize_sources

SOURCE_TERMS: dict[str, dict[str, str]] = {
    "fsq": {
        "name": "Foursquare OS Places",
        "license": "Apache-2.0",
        "attribution": "Contains data from Foursquare OS Places.",
        "terms_url": "https://docs.foursquare.com/data-products/docs/fsq-places-open-source",
    },
    "overture": {
        "name": "Overture Maps Foundation, Places theme",
        "license": "MULTI-LICENSE",
        "attribution": "Contains data from the Overture Maps Foundation.",
        "terms_url": "https://docs.overturemaps.org/attribution/",
    },
    "osm": {
        "name": "OpenStreetMap",
        "license": "ODbL-1.0",
        "attribution": "Contains data from OpenStreetMap, © OpenStreetMap contributors.",
        "terms_url": "https://www.openstreetmap.org/copyright",
    },
}

GENERAL_NOTICE = (
    "These entries record upstream source terms and provenance only. They are "
    "not a legal conclusion about the licence of the combined OpenPlaces PH "
    "output. Review DATA_LICENSES.md and the current upstream terms before "
    "publishing or redistributing a derived database."
)

OSM_NOTICE = (
    "OpenStreetMap data is licensed under ODbL 1.0 and requires attribution. "
    "Share-alike obligations may apply to a publicly distributed derivative "
    "database; evaluate the concrete release architecture before publication."
)

OVERTURE_NOTICE = (
    "Overture Places is multi-license at the upstream-provider level. OpenPlaces "
    "derives each canonical Overture member's licence label from preserved provider "
    "provenance; prefer those observed/derived values rather than assigning one "
    "blanket licence to all Overture records."
)


def source_terms(sources: Iterable[str]) -> list[dict[str, str]]:
    return [dict(SOURCE_TERMS[s], source=s) for s in normalize_sources(sources)]


def has_odbl_source(sources: Iterable[str]) -> bool:
    """Whether the selected inputs include OpenStreetMap.

    This is a provenance fact, not a conclusion that the whole output must be
    distributed under ODbL.
    """
    return "osm" in set(normalize_sources(sources))


def requires_share_alike(sources: Iterable[str]) -> bool:
    """Deprecated compatibility alias.

    Historically this name implied a legal conclusion. It now only reports
    that an ODbL source is present. Callers should use :func:`has_odbl_source`
    and read DATA_LICENSES.md before making a publication decision.
    """
    return has_odbl_source(sources)


def _observed_for(
    observed_licenses: Mapping[str, Sequence[str]] | None, source: str
) -> list[str]:
    if not observed_licenses:
        return []
    values = observed_licenses.get(source) or ()
    return sorted({str(v) for v in values if v})


def licence_block(
    sources: Iterable[str],
    releases: Mapping[str, object] | None = None,
    observed_licenses: Mapping[str, Sequence[str]] | None = None,
) -> dict:
    """Machine-readable upstream-licence record for run metadata."""
    selected = normalize_sources(sources)
    releases = releases or {}
    entries = []
    for source in selected:
        terms = SOURCE_TERMS[source]
        entry = {
            "source": source,
            "name": terms["name"],
            "declared_license": terms["license"],
            "observed_licenses": _observed_for(observed_licenses, source),
            "attribution": terms["attribution"],
            "terms_url": terms["terms_url"],
            "release": releases.get(f"{source}_release") or releases.get(source),
        }
        entries.append(entry)

    return {
        "software_license": "MIT",
        "combined_output_license": "UNDETERMINED",
        "contains_odbl_source": has_odbl_source(selected),
        "sources": entries,
        "notice": GENERAL_NOTICE,
    }


def attribution_text(
    sources: Iterable[str],
    releases: Mapping[str, object] | None = None,
    scope_name: str | None = None,
    observed_licenses: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Human-readable ATTRIBUTION.txt without making a blanket legal claim."""
    selected = normalize_sources(sources)
    releases = releases or {}
    today = _dt.date.today().isoformat()

    lines = ["OpenPlaces PH derived data attribution", ""]
    if scope_name:
        lines.append(f"Scope:     {scope_name}")
    lines.append(f"Generated: {today}")
    lines.append("")
    lines.append("Sources")
    lines.append("-------")

    for source in selected:
        terms = SOURCE_TERMS[source]
        release = releases.get(f"{source}_release") or releases.get(source)
        suffix = f" (release {release})" if release else ""
        lines.append(f"* {DISPLAY_NAME[source]}{suffix}")
        lines.append(f"  Declared licence: {terms['license']}")
        observed = _observed_for(observed_licenses, source)
        if observed:
            lines.append(f"  Observed values:  {', '.join(observed)}")
        lines.append(f"  Attribution:      {terms['attribution']}")
        lines.append(f"  Terms:            {terms['terms_url']}")
        if source == "overture":
            lines.append(f"  Note:             {OVERTURE_NOTICE}")
        elif source == "osm":
            lines.append(f"  Note:             {OSM_NOTICE}")
        lines.append("")

    lines.extend([
        "Software",
        "--------",
        "OpenPlaces PH pipeline code is MIT licensed.",
        "",
        "Publication / redistribution",
        "----------------------------",
        GENERAL_NOTICE,
        "",
    ])
    return "\n".join(lines)
