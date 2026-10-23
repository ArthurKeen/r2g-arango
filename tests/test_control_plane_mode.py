"""r2g as a tool of a context-and-governance control plane (``CONTROL_PLANE_URL``).

Under a control plane the context token is the only credential, r2g shows only the
transformation project the control-plane project opens in it, and a context from another
tenant is refused. Each refusal has a positive control. The control plane is a fake behind
``control_plane.set_instance``: its SDK is not installed here.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from r2g import control_plane
from r2g.catalog import CatalogManager
from r2g.ui.server import create_app

TOKEN = "context-for-equity"
OTHER_TENANT = "context-for-globex"
UNAVAILABLE = "context-while-down"
STATIC = Path(__file__).resolve().parents[1] / "src" / "r2g" / "ui" / "static"


class FakeControlPlane:
    def verify(self, token: str) -> control_plane.Identity:
        if token == UNAVAILABLE:
            raise control_plane.ContextUnavailable("keys could not be fetched")
        if token not in (TOKEN, OTHER_TENANT):
            raise control_plane.ContextRejected("bad signature")
        return control_plane.Identity(
            principal="pj@example.com", roles=("analyst",),
            tenant="globex" if token == OTHER_TENANT else "local",
            project="equity", ontology=("fibo-equity", "working"), token=token)

    def project(self, identity: control_plane.Identity) -> control_plane.Project:
        return control_plane.Project("equity", "Equity research", "customer-360")


@pytest.fixture
def governed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://cp.test/api")
    monkeypatch.setenv("R2G_TENANT", "local")
    monkeypatch.setenv("R2G_API_TOKEN", "r2g-own-token")
    monkeypatch.delenv("AOE_URL", raising=False)
    control_plane.set_instance(FakeControlPlane())
    catalog = CatalogManager(tmp_path)
    catalog.add_source("src", "csv", str(tmp_path))
    for name in ("customer-360", "someone-elses"):
        (tmp_path / f"{name}.yaml").write_text("collections: {}\nedges: []\n")
        catalog.create_project(name, "src", str(tmp_path / f"{name}.yaml"))
    yield TestClient(create_app(catalog_dir=str(tmp_path), host="0.0.0.0"))
    control_plane.set_instance(None)


def bearer(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestMode:
    def test_the_page_learns_the_mode_before_it_has_a_context(self, governed):
        assert governed.get("/api/mode").json() == {"control_plane": True, "home": "/"}

    def test_without_a_control_plane_nothing_changes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)
        client = TestClient(create_app(catalog_dir=str(tmp_path)))
        assert client.get("/api/mode").json() == {"control_plane": False, "home": None}
        assert client.get("/api/projects").status_code == 200       # loopback stays open
        assert client.get("/api/control-plane/context").status_code == 404


class TestTheContextIsTheOnlyCredential:
    def test_a_context_opens_the_api(self, governed):
        assert governed.get("/api/projects", headers=bearer()).status_code == 200

    def test_no_token_or_a_bad_one_is_401(self, governed):
        assert governed.get("/api/projects").status_code == 401
        assert governed.get("/api/projects", headers=bearer("forged")).status_code == 401

    def test_r2gs_own_token_no_longer_works(self, governed):
        r = governed.get("/api/projects", headers=bearer("r2g-own-token"))
        assert r.status_code == 401

    def test_an_unreachable_control_plane_is_503_never_a_way_in(self, governed):
        assert governed.get("/api/projects", headers=bearer(UNAVAILABLE)).status_code == 503

    def test_another_tenants_context_is_refused(self, governed):
        r = governed.get("/api/projects", headers=bearer(OTHER_TENANT))
        assert r.status_code == 403 and "local" in r.text

    def test_the_progress_stream_takes_the_context_as_a_query_parameter(self, governed):
        # An EventSource cannot send a header. Positive control: with ?token= the request
        # reaches the route (404: no such load), without it the middleware refuses it.
        path = "/api/projects/customer-360/load/nope/stream"
        assert governed.get(path).status_code == 401
        assert governed.get(f"{path}?token={TOKEN}").status_code == 404

    def test_the_open_paths_need_nothing(self, governed):
        assert governed.get("/api/health").status_code == 200
        assert governed.get("/").status_code == 200


class TestOnlyTheProjectsTransformationProject:
    def test_the_list_holds_only_the_one_it_opens(self, governed):
        names = [p["name"] for p in governed.get("/api/projects", headers=bearer()).json()]
        assert names == ["customer-360"]

    def test_another_one_does_not_exist(self, governed):
        assert governed.get("/api/projects/customer-360", headers=bearer()).status_code == 200
        r = governed.get("/api/projects/someone-elses", headers=bearer())
        assert r.status_code == 404
        r = governed.get("/api/projects/someone%2Delses/graph-data", headers=bearer())
        assert r.status_code == 404


class TestTheContextItself:
    def test_says_who_where_and_what_to_open(self, governed):
        ctx = governed.get("/api/control-plane/context", headers=bearer()).json()
        assert ctx["principal"] == "pj@example.com"
        assert ctx["project"] == {"id": "equity", "name": "Equity research"}
        assert ctx["opens"] == "customer-360"
        assert ctx["ontology"]["ontology_id"] == "fibo-equity"
        assert "AOE_URL" in ctx["ontology"]["problem"]          # says why there is no list


class TestTheOntologyFromAoe:
    IDENTITY = control_plane.Identity("pj@example.com", ("analyst",), "local", "equity",
                                      ("fibo-equity", "working"), token=TOKEN)

    def test_classes_are_read_with_the_users_own_context(self, monkeypatch):
        monkeypatch.setenv("AOE_URL", "http://aoe:8000/aoe/")
        http = MagicMock()
        http.get.return_value.status_code = 200
        http.get.return_value.json.return_value = {"data": [
            {"uri": "https://ex.org/Income", "label": "Income"},
            {"uri": "https://ex.org/Equity", "label": "equity"},
            {"label": "no uri, skipped"}]}
        out = control_plane.ontology_classes(self.IDENTITY, http=http)
        assert [c["label"] for c in out["classes"]] == ["equity", "Income"]
        assert out["problem"] is None
        url = http.get.call_args.args[0]
        assert url == "http://aoe:8000/aoe/api/v1/ontology/fibo-equity/classes"
        assert http.get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}

    def test_a_refusal_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setenv("AOE_URL", "http://aoe:8000/aoe")
        http = MagicMock()
        http.get.return_value.status_code = 404
        out = control_plane.ontology_classes(self.IDENTITY, http=http)
        assert out["classes"] == [] and "404" in out["problem"]


def test_the_catalog_directory_follows_r2g_catalog_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("R2G_CATALOG_DIR", str(tmp_path / "tenant-local"))
    assert CatalogManager().dir == tmp_path / "tenant-local"


def test_the_page_uses_no_root_absolute_addresses():
    """Behind a proxy that mounts r2g at /r2g/, a root-absolute /api or /static address
    misses the prefix. Every address must be relative, or built on API (the mount base).
    The one exception is the control plane's own /api/context, which lives at the root."""
    pattern = re.compile(r"""(?:fetch|EventSource)\(\s*['"`]/|(?:src|href)=["']/(?!/)""")
    offenders = []
    for path in sorted(STATIC.glob("*.htm*")) + sorted(STATIC.glob("*.js")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Exceptions, both the control plane's and at its root on purpose: renewing the
            # context, and the link home.
            if pattern.search(line) and "'/api/context'" not in line \
                    and 'id="cp-gate-home"' not in line:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == []


def test_api_fetch_keeps_the_auth_header_when_a_caller_passes_headers():
    """Spreading opts after headers used to let a caller's headers drop the token."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "{ ...opts, headers: _authHeaders(opts.headers) }" in html
    assert "{ headers: _authHeaders(opts.headers), ...opts }" not in html


