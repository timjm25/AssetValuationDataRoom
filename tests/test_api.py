"""Flask API, including the AssetProgramRegistry fetch with a mocked httpx."""
import httpx
import pytest

from valuation_room.api import create_app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ASSET_REGISTRY_URL", "http://registry.test:5101")
    return create_app().test_client()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_healthz_and_openapi(client):
    assert client.get("/healthz").get_json()["status"] == "ok"
    assert "/value" in client.get("/openapi.json").get_json()["paths"]


def test_post_value(client, cart, ov):
    r = client.post("/value?sensitivity=1", json={"lineage": cart, "overrides": ov})
    body = r.get_json()
    assert r.status_code == 200
    assert body["rnpv"] == pytest.approx(195.02, abs=0.01)
    assert body["sensitivity"]["rows"] and body["scenarios"]["bull"]["rnpv"] > body["rnpv"]
    # a bare lineage body also works
    assert client.post("/value", json=cart).status_code == 200


def test_post_value_errors_are_400(client, cart, ov):
    cart["indications"][0]["peak_sales_millions"] = None
    r = client.post("/value", json={"lineage": cart, "overrides": ov})
    assert r.status_code == 400 and "peak_sales_millions" in r.get_json()["error"]
    assert client.post("/value", data="nope", content_type="text/plain").status_code == 400


def test_post_sensitivity(client, cart, ov):
    body = client.post("/sensitivity", json={"lineage": cart, "overrides": ov}).get_json()
    assert body["sensitivity"]["base_rnpv"] == pytest.approx(195.02, abs=0.01)


def test_asset_value_fetches_registry(client, cart, monkeypatch):
    seen = {}

    def fake_get(url, timeout=None):
        seen["url"] = url
        return _Resp(200, cart)

    monkeypatch.setattr(httpx, "get", fake_get)
    r = client.get("/assets/AST-000002/value?override=as_of=2026-11-06&override=discount_rate=0.12")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert seen["url"] == "http://registry.test:5101/assets/AST-000002/lineage"
    assert body["inputs"]["discount_rate"] == 0.12
    assert body["lineage_source"].endswith("/assets/AST-000002/lineage")


def test_asset_value_registry_down_is_502(client, monkeypatch):
    def down(url, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", down)
    r = client.get("/assets/AST-000002/value")
    assert r.status_code == 502
    assert "unreachable" in r.get_json()["error"] and "registry.test" in r.get_json()["error"]


def test_asset_value_404_and_bad_id(client, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(404, {"error": "nope"}))
    assert client.get("/assets/AST-000009/value").status_code == 404
    assert client.get("/assets/BAD-1/value").status_code == 400
    monkeypatch.setattr(httpx, "get", lambda url, timeout=None: _Resp(500, {}))
    assert client.get("/assets/AST-000009/value").status_code == 502


def test_dataroom_gaps(client, cart):
    r = client.post("/dataroom/gaps", json={"lineage": cart, "documents": [
        "target_product_profile.md", "target_validation.md", "in_vitro_pharmacology.md", "notes.txt"]})
    body = r.get_json()
    assert r.status_code == 200
    missing = {g["item"] for g in body["gaps"] if g["level"] == "required"}
    assert "tpp" not in missing and {"glp_tox", "protocol", "investigator_brochure"} <= missing
    assert body["unclassified"] == ["notes.txt"]
