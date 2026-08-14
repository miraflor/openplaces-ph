from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True)
class Scope:
    name: str
    bboxes: tuple[tuple[float, float, float, float], ...]
    full_philippines: bool = False

    @property
    def slug(self) -> str:
        return self.name.replace(",", "_").replace(" ", "_")


def load_area_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_scope(config: dict, scope: str, areas: Iterable[str] | None) -> Scope:
    area_map = config["areas"]
    if areas:
        chosen = tuple(areas)
        unknown = [a for a in chosen if a not in area_map]
        if unknown:
            raise ValueError(
                f"Unknown area(s): {', '.join(unknown)}. Available: {', '.join(sorted(area_map))}"
            )
        return Scope(
            name="areas_" + "_".join(chosen),
            bboxes=tuple(tuple(map(float, area_map[a])) for a in chosen),
            full_philippines=False,
        )
    if scope != "philippines":
        raise ValueError("Only --scope philippines is supported without --areas.")
    return Scope(
        name="philippines",
        bboxes=(tuple(map(float, config["philippines_bbox"])),),
        full_philippines=True,
    )


def bbox_sql(
    lon_col: str,
    lat_col: str,
    bboxes: tuple[tuple[float, float, float, float], ...],
) -> str:
    parts = []
    for west, south, east, north in bboxes:
        parts.append(
            f"({lon_col} >= {west} AND {lon_col} < {east} "
            f"AND {lat_col} >= {south} AND {lat_col} < {north})"
        )
    return "(" + " OR ".join(parts) + ")"
