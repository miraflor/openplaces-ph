import sys
import types

import pytest


def test_foursquare_reports_gated_access_cleanly(monkeypatch):
    pytest.importorskip("pyarrow")
    sources = pytest.importorskip("openplaces_ph.sources")

    class Response:
        status_code = 403

    class GatedError(RuntimeError):
        response = Response()

    class FakeApi:
        def list_repo_tree(self, **_kwargs):
            raise GatedError("Access to this gated dataset requires approval")

    fake_hf = types.SimpleNamespace(
        HfApi=lambda: FakeApi(),
        get_token=lambda: "token-present",
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    with pytest.raises(RuntimeError, match="approved access"):
        sources._discover_fsq_release()
