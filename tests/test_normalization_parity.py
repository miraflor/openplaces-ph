"""Check Python/SQL name-normalization parity on a Unicode-stable sample."""

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


def _python_strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))


def _samples():
    ranges = [range(0x20, 0x3000), range(0xFB00, 0xFB50), range(0xFF00, 0xFFF0),
              range(0x1D400, 0x1D420)]

    chars = []
    for r in ranges:
        for cp in r:
            if 0xD800 <= cp <= 0xDFFF:
                continue
            ch = chr(cp)
            if unicodedata.category(ch) != "Cn":
                chars.append(ch)

    # Python's unicodedata and DuckDB's utf8proc are separate Unicode
    # databases. At version boundaries they can classify newly assigned
    # characters differently. Establish the subset on which the primitive
    # accent-stripping step agrees, then test the complete OpenPlaces
    # normalization pipeline on that Unicode-stable subset.
    con = duckdb.connect()
    try:
        con.register("c", pa.table({"i": list(range(len(chars))), "ch": chars}))
        duck_stripped = dict(
            con.execute("SELECT i, strip_accents(ch) FROM c").fetchall()
        )
    finally:
        con.close()

    out = list(REAL_NAMES)
    for i, ch in enumerate(chars):
        if duck_stripped[i] != _python_strip_accents(ch):
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
