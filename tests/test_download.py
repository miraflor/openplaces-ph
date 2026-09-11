"""Resumable download against a local HTTP server that behaves like Geofabrik.

The server (``upstream`` fixture in conftest.py) supports HEAD, Range,
If-Range, and 416, and it can drop the connection part-way through a body or
replace its file between two requests (Geofabrik publishes daily).
"""

from __future__ import annotations

import socket

import pytest

from openplaces_ph import util
from openplaces_ph.util import download_identity, resume_download


def _gets(up):
    return [headers for method, headers in up.log if method == "GET"]


def test_fresh_download_records_the_upstream_version(upstream, tmp_path):
    target = resume_download(upstream.url, tmp_path / "x.pbf")
    assert target.read_bytes() == upstream.content
    identity = download_identity(target)
    assert identity["etag"] == '"v1"' and identity["bytes"] == len(upstream.content)


def test_interrupted_download_resumes_the_same_version(upstream, tmp_path):
    upstream.cut_after = 30_000
    target = resume_download(upstream.url, tmp_path / "x.pbf")
    assert target.read_bytes() == upstream.content
    resume = _gets(upstream)[1]
    # Whole chunks received before the drop are kept: 3 x 8192 bytes.
    assert resume["Range"] == "bytes=24576-"
    assert resume["If-Range"] == '"v1"'


def test_a_new_upstream_version_is_never_joined_to_old_bytes(upstream, tmp_path):
    new = bytes(reversed(upstream.content))
    upstream.cut_after = 30_000
    upstream.after_cut = lambda: upstream.publish(new, '"v2"')  # daily update
    target = resume_download(upstream.url, tmp_path / "x.pbf")
    # The resume asked for the old version; the server sent the new file whole.
    assert _gets(upstream)[1]["If-Range"] == '"v1"'
    # Without If-Range this was old[:24576] + new[24576:]: correct length,
    # accepted by the length check, unreadable by Osmium.
    assert target.read_bytes() == new
    assert download_identity(target)["etag"] == '"v2"'


def test_partial_file_that_is_already_complete_is_promoted(upstream, tmp_path):
    target = tmp_path / "x.pbf"
    part = tmp_path / "x.pbf.part"
    part.write_bytes(upstream.content)
    util.atomic_json(tmp_path / "x.pbf.part.meta.json",
                     {"url": upstream.url, "etag": '"v1"', "bytes": len(upstream.content)})
    resume_download(upstream.url, target)
    assert target.read_bytes() == upstream.content
    assert len(_gets(upstream)) == 1  # one 416 answer, no body transferred again


def test_partial_file_without_a_recorded_version_restarts(upstream, tmp_path):
    (tmp_path / "x.pbf.part").write_bytes(b"left over by 0.2.0")
    target = resume_download(upstream.url, tmp_path / "x.pbf")
    assert target.read_bytes() == upstream.content
    assert "Range" not in _gets(upstream)[0]


def test_refresh_downloads_only_when_upstream_changed(upstream, tmp_path):
    target = resume_download(upstream.url, tmp_path / "x.pbf")
    resume_download(upstream.url, target, refresh=True)
    assert len(_gets(upstream)) == 1  # HEAD said "unchanged"

    new = bytes(reversed(upstream.content))
    upstream.publish(new, '"v2"')
    resume_download(upstream.url, target, refresh=True)
    assert target.read_bytes() == new
    assert len(_gets(upstream)) == 2


def test_refresh_while_offline_keeps_the_local_file(tmp_path):
    target = tmp_path / "x.pbf"
    target.write_bytes(b"good local file")
    with socket.socket() as s:  # a port with nothing listening
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(Exception):
        resume_download(f"http://127.0.0.1:{port}/x.pbf", target, refresh=True, retries=1)
    assert target.read_bytes() == b"good local file"
