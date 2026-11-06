"""ValuationSnapshot envelope, bus client, thesis-generator SOTP row, DealStructureSimulator deal."""
import csv
import io
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest

from valuation_room._vendor import thesisgen_valuation as vendored
from valuation_room.engine import value
from valuation_room.exports import (SNAPSHOT_FIELDS, SOTP_COLUMNS, BusClient, PublishRejected,
                                    dealsim_license_out, snapshot_envelope, sotp_csv, sotp_row,
                                    valuation_snapshot)

TEMPLATE = Path(__file__).parent / "fixtures" / "sotp_assets_template.csv"


def test_valuation_snapshot_fields_exactly_per_conventions(cart, ov):
    p = valuation_snapshot(value(cart, ov))
    assert list(p) == SNAPSHOT_FIELDS == [
        "asset_id", "as_of", "currency", "units", "rnpv", "npv_unrisked", "cumulative_pos",
        "peak_sales_millions", "discount_rate", "years_to_launch", "patent_expiry_year",
        "assumptions", "sources", "method"]
    assert (p["currency"], p["units"], p["method"]) == ("USD", "millions", "rNPV")
    assert p["patent_expiry_year"] == 2045 and p["as_of"] == "2026-11-06"
    assert all(set(s) == {"field", "source_system", "ref"} for s in p["sources"])


def test_envelope_correlation_and_shape(cart, ov):
    env = snapshot_envelope(value(cart, ov))
    assert env["correlation_id"] == env["payload"]["asset_id"] == "AST-000002"
    assert env["message_type"] == "ValuationSnapshot" and env["schema_version"] == "1.0.0"
    assert env["source_agent"] == "asset-valuation" and env["gxp_relevant"] is False
    uuid.UUID(env["message_id"])
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$", env["sent_at"])
    json.dumps(env)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


def test_bus_client_publishes(monkeypatch, cart, ov, tmp_path):
    env = snapshot_envelope(value(cart, ov))
    seen = {}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        return _Resp(202, {"message_id": json["message_id"]})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = BusClient("http://bus:5102", str(tmp_path / "outbox.jsonl")).publish(env)
    assert out == {"status": "published", "message_id": env["message_id"]}
    assert seen["url"] == "http://bus:5102/publish"


def test_bus_client_offline_outbox_and_flush(monkeypatch, cart, ov, tmp_path):
    env = snapshot_envelope(value(cart, ov))

    def down(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    client = BusClient("http://bus:5102", str(tmp_path / "outbox.jsonl"))
    out = client.publish(env)
    assert out["status"] == "queued_offline"
    lines = (tmp_path / "outbox.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["message_id"] == env["message_id"]
    assert client.flush() == {"sent": 0, "remaining": 1, "rejected": 0}
    monkeypatch.setattr(httpx, "post", lambda url, json=None, timeout=None: _Resp(202, {"message_id": json["message_id"]}))
    assert client.flush() == {"sent": 1, "remaining": 0, "rejected": 0}


def test_bus_client_422_raises(monkeypatch, cart, ov, tmp_path):
    env = snapshot_envelope(value(cart, ov))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(422, {"errors": [{"path": "payload.rnpv", "message": "bad"}]}))
    with pytest.raises(PublishRejected, match="payload.rnpv"):
        BusClient("http://bus:5102", str(tmp_path / "o.jsonl")).publish(env)
    assert not (tmp_path / "o.jsonl").exists()


def test_sotp_row_columns_equal_template_header(cart, ov):
    header = next(csv.reader(TEMPLATE.open()))
    row = sotp_row(value(cart, ov))
    assert list(row) == header == SOTP_COLUMNS
    text = sotp_csv([row])
    parsed = list(csv.DictReader(io.StringIO(text)))[0]
    assert parsed["stage"] == "phase_1"
    assert parsed["phase_probabilities"].startswith("phase_1=0.454545;phase_2=")


def _tg_parse_asset(row):
    """Same conversions as thesis-generator dataimport.parse_assets (commit 9bab1ef)."""
    a = {k: v for k, v in row.items() if str(v).strip() != ""}
    a["phase_probabilities"] = {k.strip(): float(x) for k, x in
                                (p.split("=", 1) for p in a["phase_probabilities"].split(";") if p.strip())}
    for k in ("rnpv", "ownership", "peak_sales_millions", "discount_rate", "cogs_pct", "opex_pct", "tax_rate"):
        if k in a:
            a[k] = float(a[k])
    for k in ("patent_life_years", "years_to_launch", "ramp_years"):
        if k in a:
            a[k] = int(float(a[k]))
    return a


def test_sotp_row_round_trips_through_thesis_generator(cart, ov):
    v = value(cart, ov)
    parsed = list(csv.DictReader(io.StringIO(sotp_csv([sotp_row(v, include_rnpv=False)]))))[0]
    assert parsed["rnpv"] == ""
    # thesis-generator recomputes from the template columns (its default COGS equals ours here)
    assert vendored.asset_value(_tg_parse_asset(parsed)) == pytest.approx(v.rnpv, rel=1e-4)
    with_rnpv = list(csv.DictReader(io.StringIO(sotp_csv([sotp_row(v, ownership=0.5)]))))[0]
    assert vendored.asset_value(_tg_parse_asset(with_rnpv)) == pytest.approx(v.rnpv * 0.5, abs=0.01)


def test_sotp_extended_columns_carry_cogs(cart, ov):
    v = value(cart, {**ov, "cogs_pct": 0.35})
    row = sotp_row(v, include_rnpv=False, extended=True)
    assert list(row)[:len(SOTP_COLUMNS)] == SOTP_COLUMNS
    assert vendored.asset_value(_tg_parse_asset(row)) == pytest.approx(v.rnpv, rel=1e-4)


# --- DealStructureSimulator compatibility: local re-implementation of
# dealsim/components.py validate_component (timjm25/DealStructureSimulator @ 2394f04)
_TYPES = {"upfront", "milestone", "royalty", "cost", "penalty"}
_REQUIRED = {
    "upfront": ["amount"],
    "milestone": ["amount", "timing_years", "probability"],
    "royalty": ["sales_low", "sales_likely", "sales_high", "royalty_pct", "years"],
    "cost": ["amount", "years"],
    "penalty": ["amount", "timing_years", "probability"],
}


def dealsim_validate_component(c):
    if not isinstance(c, dict):
        raise ValueError("each component must be an object")
    t = c.get("type")
    if t not in _TYPES:
        raise ValueError(f"unknown component type: {t!r}")
    cfg = c.get("config", {}) or {}
    missing = [f for f in _REQUIRED[t] if f not in cfg]
    if missing:
        raise ValueError(f"{t} missing {missing}")
    if t in ("milestone", "penalty") and not (0.0 <= cfg["probability"] <= 1.0):
        raise ValueError("probability")
    if t in ("royalty", "cost") and "probability" in cfg and not (0.0 <= cfg["probability"] <= 1.0):
        raise ValueError("probability")


def dealsim_create_deal_checks(name, counterparty, deal_type, discount_rate, components):
    assert name and str(name).strip() and counterparty and str(counterparty).strip()
    assert deal_type and str(deal_type).strip()
    assert isinstance(discount_rate, (int, float)) and discount_rate >= 0
    assert isinstance(components, list) and components
    for c in components:
        dealsim_validate_component(c)


def test_dealsim_components_pass_validation(cart, ov):
    out = dealsim_license_out(cart, ov)
    deal = out["create_deal"]
    dealsim_create_deal_checks(**deal)
    types = [c["type"] for c in deal["components"]]
    assert types[0] == "upfront" and types[-1] == "royalty" and types.count("milestone") == 4
    # range([1, years]) in dealsim needs an int
    assert isinstance(deal["components"][-1]["config"]["years"], int)
    json.dumps(deal)


def test_dealsim_milestone_probabilities_are_reach_probabilities(cart, ov):
    v = value(cart, ov)
    deal = dealsim_license_out(cart, ov, units="millions")["create_deal"]
    ms = [c for c in deal["components"] if c["type"] == "milestone"]
    p = v.inputs.phase_probabilities
    assert [m["label"] for m in ms] == ["Phase 2 start", "Phase 3 start", "BLA/NDA filing", "Approval"]
    assert ms[0]["config"]["probability"] == pytest.approx(p["phase_1"], abs=1e-6)
    assert ms[1]["config"]["probability"] == pytest.approx(p["phase_1"] * p["phase_2"], abs=1e-6)
    assert ms[-1]["config"]["probability"] == pytest.approx(0.10, abs=1e-6)
    timings = [m["config"]["timing_years"] for m in ms]
    assert timings == sorted(timings) and timings[-1] == v.inputs.years_to_launch


def test_dealsim_royalty_uses_bear_base_bull_and_cumulative_pos(cart, ov):
    out = dealsim_license_out(cart, ov, upfront_millions=40, milestones_millions=200, royalty_pct=0.1,
                              counterparty="EXAMPLE Licensee", units="millions")
    roy = out["create_deal"]["components"][-1]["config"]
    assert (roy["sales_low"], roy["sales_likely"], roy["sales_high"]) == (630.0, 900.0, 1170.0)
    assert roy["probability"] == pytest.approx(0.10) and roy["royalty_pct"] == 0.1
    assert out["create_deal"]["components"][0]["config"]["amount"] == 40
    assert sum(c["config"]["amount"] for c in out["create_deal"]["components"] if c["type"] == "milestone") \
        == pytest.approx(200)
    assert set(out["term_sources"].values()) == {"user_override"}
    usd = dealsim_license_out(cart, ov, upfront_millions=40)["create_deal"]
    assert usd["components"][0]["config"]["amount"] == 40_000_000


def test_dealsim_uses_registry_license_terms(cart, ov):
    cart["deals"].append({"deal_id": "DEL-000042", "asset_id": "AST-000002", "partner_id": "PTN-000003",
                          "deal_type": "license_out", "status": "term_sheet", "upfront_millions": 75,
                          "milestones_millions": 600, "royalty_pct": 12, "signed_date": None,
                          "dealsim_name": "EXAMPLE VR-201 license"})
    cart["partners"].append({"partner_id": "PTN-000003", "name": "EXAMPLE Big Pharma", "kind": "licensee"})
    out = dealsim_license_out(cart, ov, units="millions")
    deal = out["create_deal"]
    assert deal["name"] == "EXAMPLE VR-201 license" and deal["counterparty"] == "EXAMPLE Big Pharma"
    assert deal["components"][-1]["config"]["royalty_pct"] == pytest.approx(0.12)
    assert out["term_sources"]["upfront_millions"].startswith("AssetProgramRegistry DEL-000042")
