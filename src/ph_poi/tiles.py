from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Iterable


@dataclass(frozen=True)
class Tile:
    ix: int
    iy: int
    size: float

    @property
    def west(self) -> float:
        return self.ix * self.size

    @property
    def south(self) -> float:
        return self.iy * self.size

    @property
    def east(self) -> float:
        return (self.ix + 1) * self.size

    @property
    def north(self) -> float:
        return (self.iy + 1) * self.size

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    @property
    def key(self) -> str:
        # Integer grid indices are stable and avoid decimal filenames.
        tag = (f"{self.size:g}").replace(".", "p")
        return f"s{tag}_x{self.ix:+05d}_y{self.iy:+05d}"


def intersects(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    aw, as_, ae, an = a
    bw, bs, be, bn = b
    return aw < be and ae > bw and as_ < bn and an > bs


def tile_indices_for_bbox(
    bbox: tuple[float, float, float, float], size: float
) -> tuple[range, range]:
    west, south, east, north = bbox
    # Half-open tiles [west, east) / [south, north). nextafter avoids a spurious
    # tile when an AOI edge lies exactly on a grid line.
    east_in = math.nextafter(east, -math.inf)
    north_in = math.nextafter(north, -math.inf)
    ix0 = math.floor(west / size)
    ix1 = math.floor(east_in / size)
    iy0 = math.floor(south / size)
    iy1 = math.floor(north_in / size)
    return range(ix0, ix1 + 1), range(iy0, iy1 + 1)


def tiles_for_bboxes(
    bboxes: Iterable[tuple[float, float, float, float]], size: float
) -> list[Tile]:
    seen: set[tuple[int, int]] = set()
    out: list[Tile] = []
    boxes = list(bboxes)
    for bbox in boxes:
        xs, ys = tile_indices_for_bbox(bbox, size)
        for ix in xs:
            for iy in ys:
                key = (ix, iy)
                if key in seen:
                    continue
                t = Tile(ix, iy, size)
                if any(intersects(t.bbox, b) for b in boxes):
                    seen.add(key)
                    out.append(t)
    out.sort(key=lambda t: (t.iy, t.ix))
    return out


def child_tiles(parent: Tile, child_size: float) -> list[Tile]:
    if child_size <= 0 or child_size > parent.size:
        raise ValueError("child_size must be > 0 and <= parent.size")
    ratio = parent.size / child_size
    if abs(ratio - round(ratio)) > 1e-9:
        raise ValueError("source tile size must be an integer multiple of match tile size")
    return tiles_for_bboxes([parent.bbox], child_size)


def bbox_with_halo(
    bbox: tuple[float, float, float, float], meters: float
) -> tuple[float, float, float, float]:
    # Philippines is below 22 N. 1 degree longitude is still > 103 km there.
    # Using 100 km/degree is deliberately conservative for both axes.
    d = meters / 100_000.0
    w, s, e, n = bbox
    return (w - d, s - d, e + d, n + d)
