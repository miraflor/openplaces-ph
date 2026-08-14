from openplaces_ph.tiles import Tile, bbox_with_halo, child_tiles, tiles_for_bboxes


def test_source_tile_count_and_keys():
    tiles = tiles_for_bboxes([(120.1, 14.1, 121.1, 15.1)], 1.0)
    assert len(tiles) == 4
    assert all(t.key.startswith("s1_") for t in tiles)


def test_child_tiles():
    parent = Tile(120, 14, 1.0)
    kids = child_tiles(parent, 0.25)
    assert len(kids) == 16
    assert kids[0].size == 0.25


def test_halo_is_conservative():
    w, s, e, n = bbox_with_halo((120, 14, 121, 15), 120)
    assert w < 119.999 and s < 13.999
    assert e > 121.001 and n > 15.001
