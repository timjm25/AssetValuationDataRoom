"""rNPV valuation, sensitivity tornado and bear/base/bull scenarios for one asset.

The cash flow build is thesis-generator's ``rnpv_schedule`` (installed package
when importable, otherwise the vendored copy pinned at commit 9bab1ef), so a
number produced here reconciles line by line with thesis-generator.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from typing import Any, Mapping, Optional

from ._upstream import VALUATION_SOURCE, rnpv_schedule
from .inputs import InputError, RNPVInputs, build_inputs, scale_pos

#: ILLUSTRATIVE scenario multipliers (planning assumptions, not forecasts).
ILLUSTRATIVE_SCENARIOS: dict[str, dict[str, float]] = {
    "bear": {"peak_sales_factor": 0.70, "pos_factor": 0.80, "extra_delay_years": 1},
    "base": {"peak_sales_factor": 1.00, "pos_factor": 1.00, "extra_delay_years": 0},
    "bull": {"peak_sales_factor": 1.30, "pos_factor": 1.20, "extra_delay_years": 0},
}


@dataclass
class Valuation:
    """Result of valuing one asset (USD millions)."""

    asset_id: str
    asset_name: str
    stage: str
    as_of: str
    rnpv: float
    npv_unrisked: float
    cumulative_pos: float
    schedule: list[dict[str, Any]]
    inputs: RNPVInputs
    engine_source: str = VALUATION_SOURCE

    @property
    def sources(self) -> list[dict[str, str]]:
        return [asdict(s) for s in self.inputs.sources]

    @property
    def warnings(self) -> list[str]:
        return list(self.inputs.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_name": self.asset_name,
            "stage": self.stage,
            "as_of": self.as_of,
            "currency": "USD",
            "units": "millions",
            "method": "rNPV",
            "rnpv": round(self.rnpv, 4),
            "npv_unrisked": round(self.npv_unrisked, 4),
            "cumulative_pos": round(self.cumulative_pos, 6),
            "inputs": {k: v for k, v in self.inputs.to_dict().items() if k not in ("sources", "warnings")},
            "sources": self.sources,
            "warnings": self.warnings,
            "schedule": self.schedule,
            "engine_source": self.engine_source,
        }


def value_inputs(inputs: RNPVInputs) -> Valuation:
    """Value prepared inputs with thesis-generator's ``rnpv_schedule``."""
    kw = inputs.rnpv_kwargs()
    try:
        rows = rnpv_schedule(**kw)
        unrisked_kw = dict(kw, phase_probabilities={})
        unrisked = sum(r.present_value for r in rnpv_schedule(**unrisked_kw))
    except ValueError as exc:
        raise InputError(str(exc)) from exc
    schedule = [{k: round(v, 6) if isinstance(v, float) else v for k, v in asdict(r).items()} for r in rows]
    return Valuation(
        asset_id=inputs.asset_id, asset_name=inputs.asset_name, stage=inputs.stage,
        as_of=inputs.as_of.isoformat(), rnpv=sum(r.present_value for r in rows),
        npv_unrisked=unrisked, cumulative_pos=inputs.cumulative_pos, schedule=schedule, inputs=inputs,
    )


def value(asset_lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None) -> Valuation:
    """Derive inputs from a lineage record and value the asset."""
    return value_inputs(build_inputs(asset_lineage, overrides))


def _rnpv(inputs: RNPVInputs) -> float:
    return sum(r.present_value for r in rnpv_schedule(**inputs.rnpv_kwargs()))


def _with_delay(lineage: Mapping[str, Any], overrides: Mapping[str, Any], base: RNPVInputs,
                extra: int) -> RNPVInputs:
    """Re-derive inputs with extra launch delay (keeps the orphan floor logic consistent)."""
    ov = dict(overrides)
    ov["delay_years"] = int(ov.get("delay_years", 0) or 0) + extra
    ov.setdefault("as_of", base.as_of.isoformat())
    return build_inputs(lineage, ov)


def sensitivity(asset_lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None, *,
                peak_sales_pct: float = 0.30, discount_rate_pts: float = 0.02,
                pos_relative: float = 0.20, delay_years: tuple[int, int] = (1, 2)) -> dict[str, Any]:
    """One-at-a-time tornado around the base rNPV.

    Rows are sorted by swing (|high - low|), largest first. ``low``/``high`` name
    the input move, not the output direction: e.g. discount rate "high" (+2 pts)
    lowers rNPV. Launch delay has two one-sided cases (+1 and +2 years).
    """
    ov = dict(overrides or {})
    base_inputs = build_inputs(asset_lineage, ov)
    base = _rnpv(base_inputs)
    cum = base_inputs.cumulative_pos

    def case(label: str, inputs: RNPVInputs) -> dict[str, Any]:
        v = _rnpv(inputs)
        return {"label": label, "rnpv": round(v, 4), "delta": round(v - base, 4)}

    rows: list[dict[str, Any]] = []
    p = base_inputs.peak_sales_millions
    rows.append({
        "driver": "peak_sales_millions", "label": f"Peak sales +/-{peak_sales_pct:.0%}",
        "low": case(f"{p * (1 - peak_sales_pct):,.0f}", replace(base_inputs, peak_sales_millions=p * (1 - peak_sales_pct))),
        "high": case(f"{p * (1 + peak_sales_pct):,.0f}", replace(base_inputs, peak_sales_millions=p * (1 + peak_sales_pct))),
    })
    r = base_inputs.discount_rate
    rows.append({
        "driver": "discount_rate", "label": f"Discount rate +/-{discount_rate_pts * 100:.0f} pts",
        "low": case(f"{r - discount_rate_pts:.1%}", replace(base_inputs, discount_rate=r - discount_rate_pts)),
        "high": case(f"{r + discount_rate_pts:.1%}", replace(base_inputs, discount_rate=r + discount_rate_pts)),
    })
    if base_inputs.phase_probabilities:
        lo_p = scale_pos(base_inputs.phase_probabilities, cum * (1 - pos_relative))
        hi_p = scale_pos(base_inputs.phase_probabilities, cum * (1 + pos_relative))
        rows.append({
            "driver": "cumulative_pos", "label": f"Cumulative PoS +/-{pos_relative:.0%} relative",
            "low": case(f"{math.prod(lo_p.values()):.3f}", replace(base_inputs, phase_probabilities=lo_p)),
            "high": case(f"{math.prod(hi_p.values()):.3f}", replace(base_inputs, phase_probabilities=hi_p)),
        })
    d1, d2 = delay_years
    rows.append({
        "driver": "launch_delay", "label": f"Launch delay +{d1}/+{d2} years",
        "low": case(f"+{d2} yr", _with_delay(asset_lineage, ov, base_inputs, d2)),
        "high": case(f"+{d1} yr", _with_delay(asset_lineage, ov, base_inputs, d1)),
    })
    for row in rows:
        row["swing"] = round(abs(row["high"]["rnpv"] - row["low"]["rnpv"]), 4)
    rows.sort(key=lambda x: x["swing"], reverse=True)
    return {"asset_id": base_inputs.asset_id, "base_rnpv": round(base, 4), "rows": rows}


def scenarios(asset_lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Bear / base / bull rNPV using the ILLUSTRATIVE_SCENARIOS multipliers."""
    ov = dict(overrides or {})
    base_inputs = build_inputs(asset_lineage, ov)
    out: dict[str, Any] = {"asset_id": base_inputs.asset_id, "multipliers": ILLUSTRATIVE_SCENARIOS,
                           "note": "ILLUSTRATIVE scenario multipliers (planning assumptions)."}
    for name, m in ILLUSTRATIVE_SCENARIOS.items():
        inp = base_inputs
        if m["extra_delay_years"]:
            inp = _with_delay(asset_lineage, ov, base_inputs, int(m["extra_delay_years"]))
        inp = replace(inp, peak_sales_millions=base_inputs.peak_sales_millions * m["peak_sales_factor"])
        if inp.phase_probabilities and m["pos_factor"] != 1.0:
            inp = replace(inp, phase_probabilities=scale_pos(inp.phase_probabilities,
                                                             inp.cumulative_pos * m["pos_factor"]))
        out[name] = {
            "rnpv": round(_rnpv(inp), 4),
            "peak_sales_millions": round(inp.peak_sales_millions, 4),
            "cumulative_pos": round(inp.cumulative_pos, 6),
            "years_to_launch": inp.years_to_launch,
            "patent_life_years": inp.patent_life_years,
        }
    return out
