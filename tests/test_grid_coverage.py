"""The blocking grid must never silently drop in-range pairs.

A 3x3 neighbour join only guarantees coverage out to one cell width. Before
``grid_degrees`` was derived from ``max_distance_m``, a sufficiently large
search radius could silently lose genuinely in-range pairs.
"""

import math

import pytest

from openplaces_ph.matching import (
    METRES_PER_DEG_LAT_MIN,
    METRES_PER_DEG_LON_EQUATOR,
    MatchConfig,
)

# Philippine bbox latitudes, from Tawi-Tawi to the Batanes.
PH_LATITUDES = (4.4, 8.0, 14.6, 18.0, 21.3)
DISTANCES = (60.0, 120.0, 150.0, 200.0, 400.0)


@pytest.mark.parametrize("max_distance", DISTANCES)
@pytest.mark.parametrize("lat", PH_LATITUDES)
def test_one_cell_always_spans_the_search_radius(max_distance, lat):
    cfg = MatchConfig(max_distance_m=max_distance)
    lon_metres = cfg.grid_degrees * METRES_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))
    lat_metres = cfg.grid_degrees * METRES_PER_DEG_LAT_MIN
    assert lon_metres >= max_distance
    assert lat_metres >= max_distance


@pytest.mark.parametrize("max_distance", DISTANCES)
@pytest.mark.parametrize("lat", PH_LATITUDES)
def test_grid_recall_is_total(max_distance, lat):
    """Exhaustively sweep sub-cell offsets and bearings; nothing may be lost."""
    cfg = MatchConfig(max_distance_m=max_distance)
    g = cfg.grid_degrees
    m_lon = METRES_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))
    for oi in range(11):
        for oj in range(11):
            lon0 = 121.0 + g * oi / 10.0
            lat0 = lat + g * oj / 10.0
            for step in range(24):
                theta = 2 * math.pi * step / 24
                lon1 = lon0 + (max_distance * math.cos(theta)) / m_lon
                lat1 = lat0 + (max_distance * math.sin(theta)) / METRES_PER_DEG_LAT_MIN
                assert abs(math.floor(lon0 / g) - math.floor(lon1 / g)) <= 1
                assert abs(math.floor(lat0 / g) - math.floor(lat1 / g)) <= 1


@pytest.mark.parametrize("max_distance", DISTANCES)
@pytest.mark.parametrize("lat", PH_LATITUDES)
def test_prefilter_bounds_never_reject_an_in_range_pair(max_distance, lat):
    """The cheap separable box must strictly contain the true search circle."""
    cfg = MatchConfig(max_distance_m=max_distance)
    m_lon = METRES_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat))
    for step in range(72):
        theta = 2 * math.pi * step / 72
        dlon = abs(max_distance * math.cos(theta)) / m_lon
        dlat = abs(max_distance * math.sin(theta)) / METRES_PER_DEG_LAT_MIN
        assert dlat <= cfg.delta_lat_degrees
        assert dlon <= cfg.delta_lon_degrees
