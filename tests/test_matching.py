import numpy as np
import pytest

from openplaces_ph.finalize import UnionFindMask
from openplaces_ph.matching import MatchConfig, acceptance_bands, accept_pair
from openplaces_ph.util import normalize_name


def test_name_normalization():
    assert normalize_name("José's Café & Grill") == "jose s cafe and grill"


def test_conservative_acceptance():
    assert accept_pair(10, 0.90, "jollibee", "jollibee")
    assert not accept_pair(80, 0.80, "jollibee", "jolibee")
    assert not accept_pair(10, 0.95, "atm", "atm")
    assert accept_pair(10, 1.0, "atm", "atm")


def test_acceptance_is_bounded_by_max_distance():
    # Nothing beyond the configured search radius may ever be accepted.
    assert not accept_pair(130, 1.0, "jollibee", "jollibee", max_distance_m=120)
    assert accept_pair(130, 0.99, "jollibee", "jollibee", max_distance_m=200)
    assert not accept_pair(250, 1.0, "jollibee", "jollibee", max_distance_m=200)


def test_bands_truncate_without_changing_the_calibration():
    assert acceptance_bands(10.0) == ((10.0, 0.70),)
    assert acceptance_bands(30.0) == ((20.0, 0.70), (30.0, 0.82))
    assert acceptance_bands(60.0) == ((20.0, 0.70), (50.0, 0.82), (60.0, 0.90))
    assert acceptance_bands(120.0) == (
        (20.0, 0.70), (50.0, 0.82), (90.0, 0.90), (120.0, 0.94)
    )
    # A wide radius extends only the already-strict >90 m tail.
    assert acceptance_bands(200.0)[-1] == (200.0, 0.94)


def test_generic_names_still_obey_the_global_radius():
    # Generic labels normally use a 15 m cap, but a user-selected 5 m run must
    # never accept a 10 m pair just because it satisfies the generic rule.
    assert accept_pair(4.0, 1.0, "atm", "atm", max_distance_m=5.0)
    assert not accept_pair(10.0, 1.0, "atm", "atm", max_distance_m=5.0)


def test_match_config_rejects_invalid_geometry_settings():
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            MatchConfig(max_distance_m=bad)
    with pytest.raises(ValueError):
        MatchConfig(max_distance_m=120.0, safety=0.99)


def test_union_find_forbids_duplicate_source():
    # FSQ=1, Overture=2, OSM=4
    uf = UnionFindMask(np.array([1, 2, 4, 1], dtype=np.uint8))
    assert uf.union(0, 1)
    assert uf.union(1, 2)
    assert not uf.union(2, 3)  # would put two FSQ records in one cluster


def test_default_config_is_the_documented_profile():
    cfg = MatchConfig()
    assert cfg.max_distance_m == 120.0
    # Tighter than the previous hard-coded 0.0015 while still conservatively
    # covering the full 120 m search radius at Philippine latitudes.
    assert 0.0011 < cfg.grid_degrees < 0.0013
