"""The Python name normalizer must equal the SQL one that produces the data."""

import unicodedata

import duckdb
import pyarrow as pa

from openplaces_ph.sources import _sql_name_norm
from openplaces_ph.util import normalize_name

REAL_NAMES = [
    "José's Café & Grill", "Niño's Carinderia", "7‑Eleven", "Mang Inasal (SM North)",
    "Café—Bar", "Ångström", "İloilo Supermart", "ＳＭ Ｍａｌｌ", "ﬁesta mart", "Straße 7",
    "K² Studio", "新光 Shin Kong", "", "   ", None,
]


def _samples():
    ranges = [range(0x20, 0x3000), range(0xFB00, 0xFB50), range(0xFF00, 0xFFF0),
              range(0x1D400, 0x1D420)]
    out = list(REAL_NAMES)
    for r in ranges:
        for cp in r:
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            # DuckDB's utf8proc may use a newer Unicode version than this
            # Python (3.11 ships Unicode 14.0). A character unassigned in
            # Python's database is not a disagreement about the rules.
            if unicodedata.category(ch) == "Cn":
                continue
            out += [f"a{ch}b", ch, f"X{ch}"]
    return out


def test_python_mirror_equals_sql_on_a_wide_unicode_sample():
    samples = _samples()
    con = duckdb.connect()
    try:
        con.register("s", pa.table({"i": list(range(len(samples))), "n": samples}))
        sql = dict(con.execute(f"SELECT i, {_sql_name_norm('n')} FROM s").fetchall())
    finally:
        con.close()
    mismatches = [
        (samples[i], sql[i], normalize_name(samples[i]))
        for i in range(len(samples))
        if (sql[i] or "") != normalize_name(samples[i])
    ]
    assert not mismatches, mismatches[:10]


def test_documented_lossy_cases_stay_visible():
    # These are properties of the SQL normalizer, not wishes. If a future
    # normalizer changes them, this test should be updated deliberately.
    assert normalize_name("ＳＭ Ｍａｌｌ") == ""
    assert normalize_name("ﬁesta mart") == "esta mart"
    assert normalize_name("新光") == ""
