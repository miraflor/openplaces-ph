import numpy as np

from ph_poi.finalize import UnionFindMask
from ph_poi.matching import accept_pair
from ph_poi.util import normalize_name


def test_name_normalization():
    assert normalize_name("José's Café & Grill") == "jose s cafe and grill"


def test_conservative_acceptance():
    assert accept_pair(10, 0.90, "jollibee", "jollibee")
    assert not accept_pair(80, 0.80, "jollibee", "jolibee")
    assert not accept_pair(10, 0.95, "atm", "atm")
    assert accept_pair(10, 1.0, "atm", "atm")


def test_union_find_forbids_duplicate_source():
    # FSQ=1, Overture=2, OSM=4
    uf = UnionFindMask(np.array([1, 2, 4, 1], dtype=np.uint8))
    assert uf.union(0, 1)
    assert uf.union(1, 2)
    assert not uf.union(2, 3)  # would put two FSQ records in one cluster
