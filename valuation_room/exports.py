"""Exports: PharmaEventBus ValuationSnapshot, thesis-generator SOTP row, DealStructureSimulator deal.

* ``valuation_snapshot`` / ``snapshot_envelope`` build the ValuationSnapshot payload
  exactly per CONVENTIONS.md and wrap it in a MessageEnvelope 1.0.0.
* ``BusClient`` POSTs an envelope to PharmaEventBus ``/publish``; when the bus is
  unreachable it appends the envelope to a local JSONL outbox for later ``flush``.
* ``sotp_row`` / ``sotp_csv`` emit thesis-generator's ``sotp_assets_template.csv`` columns.
* ``dealsim_license_out`` emits ``create_deal`` arguments for DealStructureSimulator.
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

from .engine import Valuation, scenarios, value
from .inputs import REMAINING_PHASES, InputError

SOURCE_AGENT = "asset-valuation"
SCHEMA_VERSION = "1.0.0"
DEFAULT_BUS_URL = "http://localhost:5102"

#: thesis-generator data/templates/sotp_assets_template.csv header (commit 9bab1ef)
SOTP_COLUMNS = [
    "name", "stage", "rnpv", "ownership", "peak_sales_millions", "phase_probabilities",
    "discount_rate", "patent_life_years", "years_to_launch",
]

SNAPSHOT_FIELDS = [
    "asset_id", "as_of", "currency", "units", "rnpv", "npv_unrisked", "cumulative_pos",
    "peak_sales_millions", "discount_rate", "years_to_launch", "patent_expiry_year",
    "assumptions", "sources", "method",
]


# ------------------------------------------------------------ ValuationSnapshot

def valuation_snapshot(valuation: Valuation) -> dict[str, Any]:
    """ValuationSnapshot payload (CONVENTIONS.md), money in USD millions."""
    inp = valuation.inputs
    assumptions = {
        "stage": inp.stage,
        "modality": inp.modality,
        "phase_probabilities": {k: round(v, 6) for k, v in inp.phase_probabilities.items()},
        "patent_life_years": inp.patent_life_years,
        "patent_expiry_estimated": inp.patent_expiry_estimated,
        "orphan_floor_applied": inp.orphan_floor_applied,
        "launch_date": inp.launch_date.isoformat() if inp.launch_date else None,
        "delay_years": inp.delay_years,
        "cogs_pct": round(inp.cogs_pct, 6),
        "opex_pct": inp.opex_pct,
        "tax_rate": inp.tax_rate,
        "ramp_years": inp.ramp_years,
        "erosion_per_year": inp.erosion_per_year,
        "erosion_years": inp.erosion_years,
        "phase_costs": inp.phase_costs,
        "engine": valuation.engine_source,
        "warnings": valuation.warnings,
    }
    payload = {
        "asset_id": valuation.asset_id,
        "as_of": valuation.as_of,
        "currency": "USD",
        "units": "millions",
        "rnpv": round(valuation.rnpv, 4),
        "npv_unrisked": round(valuation.npv_unrisked, 4),
        "cumulative_pos": round(valuation.cumulative_pos, 6),
        "peak_sales_millions": inp.peak_sales_millions,
        "discount_rate": inp.discount_rate,
        "years_to_launch": inp.years_to_launch,
        "patent_expiry_year": inp.patent_expiry_year,
        "assumptions": assumptions,
        "sources": valuation.sources,
        "method": "rNPV",
    }
    assert list(payload) == SNAPSHOT_FIELDS
    return payload


def make_envelope(message_type: str, payload: Mapping[str, Any], *, source_agent: str = SOURCE_AGENT,
                  target_agent: Optional[str] = None, causation_id: Optional[str] = None,
                  gxp_relevant: bool = False) -> dict[str, Any]:
    """MessageEnvelope 1.0.0 with correlation_id = payload.asset_id (pharma contract rule)."""
    asset_id = payload.get("asset_id")
    if not asset_id:
        raise ValueError("pharma contract payloads require asset_id")
    env: dict[str, Any] = {
        "message_id": str(uuid.uuid4()),
        "message_type": message_type,
        "schema_version": SCHEMA_VERSION,
        "source_agent": source_agent,
        "correlation_id": asset_id,
        "sent_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "gxp_relevant": gxp_relevant,
        "payload": dict(payload),
    }
    if target_agent:
        env["target_agent"] = target_agent
    if causation_id:
        env["causation_id"] = causation_id
    return env


def snapshot_envelope(valuation: Valuation, **kwargs: Any) -> dict[str, Any]:
    return make_envelope("ValuationSnapshot", valuation_snapshot(valuation), **kwargs)


class PublishRejected(Exception):
    """The bus answered 422: the envelope is invalid and was not queued."""

    def __init__(self, errors: list[dict[str, Any]]):
        super().__init__("; ".join(f"{e.get('path')}: {e.get('message')}" for e in errors) or "rejected")
        self.errors = errors


@dataclass
class BusClient:
    """PharmaEventBus client with an offline JSONL outbox fallback."""

    base_url: Optional[str] = None
    outbox_path: Optional[str] = None
    timeout: float = 5.0

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or os.environ.get("PHARMA_BUS_URL") or DEFAULT_BUS_URL).rstrip("/")
        self.outbox_path = self.outbox_path or os.environ.get("VALUATION_OUTBOX", "data/outbox.jsonl")

    def _queue(self, envelope: Mapping[str, Any], reason: str) -> dict[str, Any]:
        path = Path(str(self.outbox_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(envelope, sort_keys=True) + "\n")
        return {"status": "queued_offline", "message_id": envelope.get("message_id"),
                "outbox": str(path), "reason": reason}

    def publish(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """POST to ``{bus}/publish``; queue locally if the bus is down or erroring."""
        try:
            resp = httpx.post(f"{self.base_url}/publish", json=dict(envelope), timeout=self.timeout)
        except httpx.HTTPError as exc:
            return self._queue(envelope, f"bus unreachable: {exc.__class__.__name__}")
        if resp.status_code in (200, 201, 202):
            body = resp.json() if resp.content else {}
            return {"status": "published", "message_id": body.get("message_id", envelope.get("message_id"))}
        if resp.status_code == 422:
            raise PublishRejected(resp.json().get("errors", []))
        return self._queue(envelope, f"bus returned HTTP {resp.status_code}")

    def flush(self) -> dict[str, int]:
        """Retry every queued envelope; keep the ones that still fail."""
        path = Path(str(self.outbox_path))
        if not path.exists():
            return {"sent": 0, "remaining": 0, "rejected": 0}
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        path.unlink()
        sent = rejected = 0
        for ln in lines:
            try:
                result = self.publish(json.loads(ln))
            except PublishRejected:
                rejected += 1
                continue
            if result["status"] == "published":
                sent += 1
        remaining = len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]) if path.exists() else 0
        return {"sent": sent, "remaining": remaining, "rejected": rejected}


# ------------------------------------------------------------ thesis-generator

def _fmt(v: float) -> str:
    return f"{v:.6g}"


#: Optional columns thesis-generator's importer also accepts (dataimport.ASSET_COLS, commit 9bab1ef).
SOTP_EXTENDED_COLUMNS = ["ramp_years", "cogs_pct", "opex_pct", "tax_rate"]


def sotp_row(valuation: Valuation, *, ownership: float = 1.0, include_rnpv: bool = True,
             extended: bool = False) -> dict[str, str]:
    """One row for thesis-generator's SOTP assets CSV (template columns, in order).

    ``rnpv`` is filled by default so thesis-generator's ``asset_value`` uses this
    tool's figure, which carries assumptions (COGS, ramp, erosion) that the
    template has no columns for. The rNPV inputs are still filled for transparency.
    ``extended=True`` appends ramp_years, cogs_pct, opex_pct and tax_rate, which
    thesis-generator's importer also accepts, so a blank-rnpv row recomputes exactly.
    """
    inp = valuation.inputs
    row = {
        "name": f"{inp.asset_name} ({inp.asset_id})",
        "stage": inp.tg_phase,
        "rnpv": f"{valuation.rnpv:.2f}" if include_rnpv else "",
        "ownership": _fmt(ownership),
        "peak_sales_millions": _fmt(inp.peak_sales_millions),
        "phase_probabilities": ";".join(f"{k}={_fmt(v)}" for k, v in inp.phase_probabilities.items()),
        "discount_rate": _fmt(inp.discount_rate),
        "patent_life_years": str(inp.patent_life_years),
        "years_to_launch": str(inp.years_to_launch),
    }
    assert list(row) == SOTP_COLUMNS
    if extended:
        row.update({"ramp_years": str(inp.ramp_years), "cogs_pct": _fmt(inp.cogs_pct),
                    "opex_pct": _fmt(inp.opex_pct), "tax_rate": _fmt(inp.tax_rate)})
    return row


def sotp_csv(rows: list[dict[str, str]], header: bool = True) -> str:
    """CSV text; template columns, plus the extended ones when any row has them."""
    cols = SOTP_COLUMNS + (SOTP_EXTENDED_COLUMNS if any("cogs_pct" in r for r in rows) else [])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    if header:
        w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


# ------------------------------------------------------- DealStructureSimulator

#: ILLUSTRATIVE license-out term structure used only when neither the registry
#: nor the caller gives terms. Planning placeholders, not market benchmarks.
ILLUSTRATIVE_TERMS = {"upfront_pct_of_rnpv": 0.25, "milestones_multiple_of_upfront": 3.0, "royalty_pct": 0.12}

#: ILLUSTRATIVE relative phase durations used to place milestones within years_to_launch.
ILLUSTRATIVE_PHASE_DURATIONS = {"preclinical": 1.5, "ind": 0.5, "phase_1": 1.5, "phase_2": 2.0,
                                "phase_3": 2.5, "approval": 1.0}

#: Milestone event reached on passing each step, and its ILLUSTRATIVE share of milestone value.
MILESTONE_EVENTS = {
    "preclinical": ("Phase 1 start (first patient dosed)", 0.05),
    "ind": ("Phase 1 start (first patient dosed)", 0.05),
    "phase_1": ("Phase 2 start", 0.10),
    "phase_2": ("Phase 3 start", 0.20),
    "phase_3": ("BLA/NDA filing", 0.15),
    "approval": ("Approval", 0.50),
}


def _registry_terms(lineage: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    for d in lineage.get("deals") or []:
        if d.get("deal_type") == "license_out" and d.get("status") != "terminated":
            return d
    return None


def dealsim_license_out(lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None, *,
                        upfront_millions: Optional[float] = None, milestones_millions: Optional[float] = None,
                        royalty_pct: Optional[float] = None, counterparty: Optional[str] = None,
                        name: Optional[str] = None, units: str = "usd") -> dict[str, Any]:
    """Proposed license-out as DealStructureSimulator ``create_deal`` arguments.

    * upfront at year 0;
    * one milestone per remaining phase transition, probability = probability
      of REACHING that phase (product of the conditional probabilities before it),
      timed by spreading years_to_launch over ILLUSTRATIVE_PHASE_DURATIONS;
    * royalty with sales_low/likely/high = bear/base/bull peak sales,
      probability = cumulative PoS, years = exclusive years on market.

    ``units="usd"`` (default) matches DealStructureSimulator's seed data (whole
    dollars); ``units="millions"`` keeps USD millions.
    """
    if units not in ("usd", "millions"):
        raise ValueError("units must be 'usd' or 'millions'")
    scale = 1_000_000.0 if units == "usd" else 1.0
    val = value(lineage, overrides)
    inp = val.inputs
    scen = scenarios(lineage, overrides)
    notes: list[str] = []
    term_sources: dict[str, str] = {}

    reg = _registry_terms(lineage)
    partner_names = {p.get("partner_id"): p.get("name") for p in lineage.get("partners") or []}

    def pick(arg: Optional[float], reg_key: str, fallback: float, label: str) -> float:
        if arg is not None:
            term_sources[label] = "user_override"
            return float(arg)
        if reg is not None and reg.get(reg_key) not in (None, ""):
            term_sources[label] = f"AssetProgramRegistry {reg.get('deal_id')}.{reg_key}"
            return float(reg[reg_key])
        term_sources[label] = "illustrative_default"
        return fallback

    up = pick(upfront_millions, "upfront_millions",
              round(max(0.0, val.rnpv) * ILLUSTRATIVE_TERMS["upfront_pct_of_rnpv"], 1), "upfront_millions")
    ms_total = pick(milestones_millions, "milestones_millions",
                    round(up * ILLUSTRATIVE_TERMS["milestones_multiple_of_upfront"], 1), "milestones_millions")
    roy = pick(royalty_pct, "royalty_pct", ILLUSTRATIVE_TERMS["royalty_pct"], "royalty_pct")
    if roy > 1.0:
        notes.append(f"royalty_pct {roy} read as a percentage and converted to {roy / 100:.4f}.")
        roy = roy / 100.0
    if not 0.0 <= roy <= 1.0:
        raise InputError("royalty_pct must be between 0 and 1")
    if "illustrative_default" in term_sources.values():
        notes.append("Some deal terms are ILLUSTRATIVE placeholders (ILLUSTRATIVE_TERMS); replace with proposed terms.")

    components: list[dict[str, Any]] = [
        {"type": "upfront", "label": "signing upfront", "config": {"amount": round(up * scale, 2)}},
    ]

    remaining = REMAINING_PHASES.get(inp.stage, [])
    steps = [k for k in remaining if k in inp.phase_probabilities] or list(inp.phase_probabilities)
    total_w = sum(ILLUSTRATIVE_PHASE_DURATIONS.get(k, 1.0) for k in steps) or 1.0
    events = []
    reach = 1.0
    elapsed = 0.0
    for k in steps:
        reach *= inp.phase_probabilities[k]
        elapsed += ILLUSTRATIVE_PHASE_DURATIONS.get(k, 1.0)
        label, weight = MILESTONE_EVENTS.get(k, (f"{k} success", 0.1))
        events.append((label, weight, reach, inp.years_to_launch * elapsed / total_w))
    wsum = sum(e[1] for e in events) or 1.0
    for label, weight, prob, timing in events:
        components.append({
            "type": "milestone", "label": label,
            "config": {"amount": round(ms_total * weight / wsum * scale, 2),
                       "timing_years": round(timing, 2), "probability": round(min(1.0, prob), 6)},
        })

    exclusive_years = max(1, inp.patent_life_years - inp.years_to_launch)
    components.append({
        "type": "royalty", "label": "net sales royalty",
        "config": {
            "sales_low": round(scen["bear"]["peak_sales_millions"] * scale, 2),
            "sales_likely": round(scen["base"]["peak_sales_millions"] * scale, 2),
            "sales_high": round(scen["bull"]["peak_sales_millions"] * scale, 2),
            "royalty_pct": round(roy, 6),
            "years": int(exclusive_years),
            "ramp_years": int(inp.ramp_years),
            "probability": round(inp.cumulative_pos, 6),
            "start_year": inp.years_to_launch + 1,
        },
    })
    notes.append(
        "DealStructureSimulator (commit 2394f04) starts royalty cash flows in year 1 and ignores "
        "'start_year'; until it honours start_year its expected NPV overstates the royalty's present value."
    )

    if counterparty is None and reg is not None:
        counterparty = partner_names.get(reg.get("partner_id")) or reg.get("partner_id")
    counterparty = counterparty or "Prospective licensee (TBD)"
    deal_name = name or (reg.get("dealsim_name") if reg and reg.get("dealsim_name") else None) \
        or f"License-out - {inp.asset_name} ({inp.asset_id})"
    return {
        "create_deal": {
            "name": deal_name, "counterparty": counterparty, "deal_type": "license_out",
            "discount_rate": inp.discount_rate, "components": components,
        },
        "units": "USD" if units == "usd" else "USD millions",
        "term_sources": term_sources,
        "asset_rnpv_millions": round(val.rnpv, 4),
        "notes": notes,
    }
