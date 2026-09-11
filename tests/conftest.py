"""Shared fixtures: a throw-away project root with normalized source tiles.

Nothing here touches the network. Tiles are written in the same normalized
schema that acquisition produces, with release pins and directory manifests,
so the matching and finalization stages run exactly as in a real project.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from openplaces_ph import util
from openplaces_ph.tiles import Tile
from openplaces_ph.util import normalize_name

SOURCE_FIELDS = [
    ("source", pa.string()), ("source_id", pa.string()), ("name", pa.string()),
    ("category", pa.string()), ("lon", pa.float64()), ("lat", pa.float64()),
    ("provenance", pa.string()), ("upstream_license", pa.string()),
    ("name_norm", pa.string()), ("name_tokens", pa.string()),
]

_PIN_FOLDER = {"fsq": "foursquare", "overture": "overture"}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class SyntheticProject:
    """A project root laid out like a real run, filled with synthetic tiles."""

    def __init__(self, root: Path, scope_slug: str = "test") -> None:
        self.root = root
        self.fsq_dir = root / "data" / "sources" / scope_slug / "fsq"
        self.overture_dir = root / "data" / "sources" / scope_slug / "overture"
        self.osm_dir = root / "data" / "cache" / "osm" / "tiles_1p0deg"
        self.temp_dir = root / "tmp"
        self.set_release("fsq", "2026-01-01")
        self.set_release("overture", "2026-01-01.0")
        self.set_osm_snapshot("v1")

    def directory(self, source: str) -> Path:
        return {"fsq": self.fsq_dir, "overture": self.overture_dir}[source]

    def pin(self, source: str, release: str) -> None:
        """What resolve_*_release writes."""
        _write_json(self.root / "data" / "cache" / _PIN_FOLDER[source] / "release.json",
                    {"release": release})

    def bind(self, source: str, release: str) -> None:
        """What bind_dir_to_release writes."""
        _write_json(self.directory(source) / "_release.json", {"release": release})

    def set_release(self, source: str, release: str) -> None:
        self.pin(source, release)
        self.bind(source, release)

    def set_osm_snapshot(self, version: str) -> None:
        _write_json(self.osm_dir / "_SUCCESS.json",
                    {"source_tile_deg": 1.0, "pbf": {"etag": f'"{version}"'}})

    def tile(self, source: str, tile: Tile, rows) -> Path:
        """Write one normalized tile. ``rows``: (id, name, lon, lat[, provenance])."""
        if source == "osm":
            path = self.osm_dir / f"tile_x={tile.ix}" / f"tile_y={tile.iy}" / "data_0.parquet"
        else:
            path = self.directory(source) / f"{tile.key}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        norms = [normalize_name(r[1]) for r in rows]
        table = pa.table({
            "source": [source] * len(rows),
            "source_id": [r[0] for r in rows],
            "name": [r[1] for r in rows],
            "category": ["shop"] * len(rows),
            "lon": [float(r[2]) for r in rows],
            "lat": [float(r[3]) for r in rows],
            "provenance": [r[4] if len(r) > 4 else None for r in rows],
            "upstream_license": ["test"] * len(rows),
            "name_norm": norms,
            "name_tokens": [" ".join(sorted(n.split(" "))) for n in norms],
        }, schema=pa.schema(SOURCE_FIELDS))
        pq.write_table(table, path)
        return path


@pytest.fixture
def project(tmp_path: Path) -> SyntheticProject:
    return SyntheticProject(tmp_path / "project")


# ---------------------------------------------------------------------------
# A local HTTP server that behaves like download.geofabrik.de
# ---------------------------------------------------------------------------

class Upstream:
    def __init__(self, content: bytes, etag: str) -> None:
        self.content = content
        self.etag = etag
        self.last_modified = "Wed, 09 Sep 2026 20:00:00 GMT"
        self.cut_after: int | None = None     # drop the next body after N bytes
        self.after_cut = None                 # callable run after a dropped body
        self.fail_status: int | None = None   # answer every GET with this status
        self.log: list[tuple[str, dict]] = []

    def publish(self, content: bytes, etag: str) -> None:
        self.content, self.etag = content, etag
        self.last_modified = "Thu, 10 Sep 2026 20:00:00 GMT"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _common_headers(self, up: Upstream) -> None:
        self.send_header("ETag", up.etag)
        self.send_header("Last-Modified", up.last_modified)
        self.send_header("Accept-Ranges", "bytes")

    def do_HEAD(self):
        up = self.server.upstream
        up.log.append(("HEAD", dict(self.headers)))
        self.send_response(200)
        self._common_headers(up)
        self.send_header("Content-Length", str(len(up.content)))
        self.end_headers()

    def do_GET(self):
        up = self.server.upstream
        up.log.append(("GET", dict(self.headers)))
        if up.fail_status:
            self.send_response(up.fail_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, start, status = up.content, 0, 200
        wanted = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        if wanted and (if_range is None or if_range in (up.etag, up.last_modified)):
            start = int(wanted.split("=")[1].split("-")[0])
            if start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        payload = body[start:]
        self.send_response(status)
        self._common_headers(up)
        self.send_header("Content-Length", str(len(payload)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        if up.cut_after is not None:
            self.wfile.write(payload[: up.cut_after])
            self.wfile.flush()
            up.cut_after = None
            self.connection.shutdown(socket.SHUT_RDWR)
            if up.after_cut:
                up.after_cut()
                up.after_cut = None
            return
        self.wfile.write(payload)


@pytest.fixture
def upstream(monkeypatch):
    monkeypatch.setattr(util.time, "sleep", lambda seconds: None)
    # Small chunks, so a dropped connection leaves bytes in the .part file.
    monkeypatch.setattr(util, "DOWNLOAD_CHUNK_BYTES", 8192)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.upstream = Upstream(bytes(range(256)) * 400, '"v1"')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.upstream.url = f"http://127.0.0.1:{server.server_address[1]}/philippines-latest.osm.pbf"
    yield server.upstream
    server.shutdown()
    server.server_close()
