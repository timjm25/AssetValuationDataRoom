# ---------------------------------------------------------------------------
# VENDORED COPY - do not edit by hand.
# Source: timjm25/thesis-generator, file thesisgen/valuation.py, commit 9bab1ef
# Copied unchanged below this header so CI can run without the private repo.
# At runtime valuation_room prefers the installed `thesisgen.valuation` when it
# is importable. See vendor/UPSTREAM.md for refresh instructions.
# ---------------------------------------------------------------------------
"""Valuation framework components: rNPV, SOTP, comparable multiples, freshness index.

All money values are USD millions. Years are counted from "year 0" = today.

The models are deliberately transparent: every assumption is an argument with a
documented default, and ``rnpv_schedule`` returns the year-by-year build so the
number can be checked line by line in a spreadsheet.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional, Union

# Default order used to read phase probabilities when a plain dict is given.
PHASE_ORDER = [
    "preclinical", "ind", "phase_1", "phase_2", "phase_3", "nda", "bla",
    "filing", "approval",
]


def _ordered_phases(phase_probabilities: dict[str, float]) -> list[tuple[str, float]]:
    """Return (phase, probability) pairs in development order.

    Known phase names are sorted by PHASE_ORDER; unknown names keep their given
    order after the known ones.
    """
    known = [(p, phase_probabilities[p]) for p in PHASE_ORDER if p in phase_probabilities]
    unknown = [(p, v) for p, v in phase_probabilities.items() if p not in PHASE_ORDER]
    pairs = known + unknown
    for name, prob in pairs:
        if not 0.0 <= float(prob) <= 1.0:
            raise ValueError(f"Probability for '{name}' must be between 0 and 1, got {prob}")
    return [(n, float(p)) for n, p in pairs]


def cumulative_pos(phase_probabilities: dict[str, float]) -> float:
    """Cumulative probability of success = product of the phase probabilities."""
    if not phase_probabilities:
        return 1.0
    return math.prod(p for _, p in _ordered_phases(phase_probabilities))


def _ramp_share(k: int, ramp_years: int) -> float:
    """Share of peak sales in on-market year k (1-based); ~1.0 at k == ramp_years."""
    if k < 1:
        return 0.0
    return min(1.0, (1 - math.exp(-3.0 * k / ramp_years)) / (1 - math.exp(-3.0)))


@dataclass
class YearRow:
    year: int
    revenue: float
    operating_profit: float
    after_tax_cash_flow: float
    probability: float
    dev_cost: float
    risk_adjusted_cash_flow: float
    discount_factor: float
    present_value: float


def rnpv_schedule(
    peak_sales_millions: float,
    phase_probabilities: dict[str, float],
    discount_rate: float,
    patent_life_years: int,
    *,
    years_to_launch: int = 3,
    ramp_years: int = 5,
    cogs_pct: float = 0.15,
    opex_pct: float = 0.25,
    tax_rate: float = 0.21,
    erosion_per_year: float = 0.6,
    erosion_years: int = 3,
    phase_costs: Optional[dict[str, float]] = None,
) -> list[YearRow]:
    """Build the year-by-year risk-adjusted cash flow table behind ``rnpv``.

    Revenue model:
      * launch in year ``years_to_launch + 1``;
      * S-curve ramp to ``peak_sales_millions`` over ``ramp_years``;
      * flat at peak until loss of exclusivity (``patent_life_years`` from today);
      * after LoE, sales fall by ``erosion_per_year`` each year for ``erosion_years``.

    Costs: ``cogs_pct`` and ``opex_pct`` of revenue, then ``tax_rate`` on positive
    operating profit. Commercial cash flows are weighted by the cumulative PoS.

    ``phase_costs`` (optional) maps phase -> remaining cost of that phase. Phase
    costs fall one phase per year from year 1 to ``years_to_launch`` (extra
    phases share the last year) and are weighted by the probability of reaching
    that phase.
    """
    if peak_sales_millions < 0:
        raise ValueError("peak_sales_millions must be >= 0")
    if discount_rate <= -1:
        raise ValueError("discount_rate must be greater than -1")
    if patent_life_years < 0 or years_to_launch < 0 or ramp_years < 1:
        raise ValueError("patent_life_years and years_to_launch must be >= 0 and ramp_years >= 1")
    for name, pct in (("cogs_pct", cogs_pct), ("opex_pct", opex_pct), ("tax_rate", tax_rate),
                      ("erosion_per_year", erosion_per_year)):
        if not 0.0 <= pct <= 1.0:
            raise ValueError(f"{name} must be between 0 and 1")

    phases = _ordered_phases(phase_probabilities or {})
    pos = math.prod(p for _, p in phases) if phases else 1.0

    # Probability of *reaching* each phase (before that phase's own risk).
    reach = {}
    running = 1.0
    for name, prob in phases:
        reach[name] = running
        running *= prob

    dev_by_year: dict[int, float] = {}
    if phase_costs:
        phase_names = [p for p, _ in phases]
        cost_phases = [p for p in phase_names if p in phase_costs] + [
            p for p in phase_costs if p not in phase_names
        ]
        span = max(1, years_to_launch)
        for i, name in enumerate(cost_phases):
            yr = min(i + 1, span)
            dev_by_year[yr] = dev_by_year.get(yr, 0.0) + float(phase_costs[name]) * reach.get(name, 1.0)

    launch_year = years_to_launch + 1
    loe_year = patent_life_years  # last exclusive year
    last_year = max(launch_year, loe_year) + erosion_years
    loe_share = _ramp_share(loe_year - launch_year + 1, ramp_years)

    rows: list[YearRow] = []
    for year in range(0, last_year + 1):
        revenue = 0.0
        if year >= launch_year:
            if year <= loe_year:
                revenue = peak_sales_millions * _ramp_share(year - launch_year + 1, ramp_years)
            else:
                years_post = year - loe_year
                if years_post <= erosion_years:
                    revenue = peak_sales_millions * loe_share * (1 - erosion_per_year) ** years_post
        op = revenue * (1 - cogs_pct - opex_pct)
        after_tax = op - max(0.0, op) * tax_rate
        dev = dev_by_year.get(year, 0.0)
        risk_adj = after_tax * pos - dev
        df = 1.0 / (1.0 + discount_rate) ** year
        rows.append(YearRow(year, revenue, op, after_tax, pos, dev, risk_adj, df, risk_adj * df))
    return rows


def rnpv(
    peak_sales_millions: float,
    phase_probabilities: dict[str, float],
    discount_rate: float,
    patent_life_years: int,
    **assumptions,
) -> float:
    """Calculate risk-adjusted NPV in USD millions.

    Args:
        peak_sales_millions: Expected annual peak sales (USD millions)
        phase_probabilities: Probability of success by phase
            (e.g., {"phase_2": 0.50, "phase_3": 0.65, "approval": 0.85})
        discount_rate: Annual discount rate (e.g., 0.10 for 10%)
        patent_life_years: Years of patent protection remaining
        **assumptions: optional overrides passed to ``rnpv_schedule``
            (years_to_launch, ramp_years, cogs_pct, opex_pct, tax_rate,
            erosion_per_year, erosion_years, phase_costs)

    Returns:
        Risk-adjusted NPV in USD millions
    """
    rows = rnpv_schedule(peak_sales_millions, phase_probabilities, discount_rate,
                         patent_life_years, **assumptions)
    return sum(r.present_value for r in rows)


def schedule_as_dicts(rows: Iterable[YearRow]) -> list[dict]:
    return [asdict(r) for r in rows]


# --------------------------------------------------------------------------- SOTP

RNPV_KEYS = ("peak_sales_millions", "phase_probabilities", "discount_rate", "patent_life_years")
OPTIONAL_KEYS = ("years_to_launch", "ramp_years", "cogs_pct", "opex_pct", "tax_rate",
                 "erosion_per_year", "erosion_years", "phase_costs")


def asset_value(asset: dict) -> float:
    """Value of one asset: its 'rnpv' if given, else computed from rNPV inputs.

    The value is multiplied by 'ownership' (economic share, default 1.0), e.g.
    0.5 for a 50/50 co-development or 0.12 for a royalty-only interest.
    """
    if asset.get("rnpv") not in (None, ""):
        value = float(asset["rnpv"])
    elif all(asset.get(k) not in (None, "") for k in RNPV_KEYS):
        extra = {k: asset[k] for k in OPTIONAL_KEYS if asset.get(k) not in (None, "")}
        value = rnpv(float(asset["peak_sales_millions"]), asset["phase_probabilities"],
                     float(asset["discount_rate"]), int(float(asset["patent_life_years"])), **extra)
    else:
        name = asset.get("name", "<unnamed>")
        raise ValueError(f"Asset '{name}' needs an 'rnpv' value or all of {', '.join(RNPV_KEYS)}")
    own = asset.get("ownership")
    ownership = 1.0 if own in (None, "") else float(own)
    return value * ownership


def sotp_breakdown(assets: list[dict], net_cash: float = 0.0,
                   corporate_costs_npv: float = 0.0) -> dict:
    """Sum-of-the-parts with a per-asset breakdown."""
    parts = [{"name": a.get("name", "<unnamed>"), "stage": a.get("stage"), "value": asset_value(a)}
             for a in assets]
    pipeline = sum(p["value"] for p in parts)
    total = pipeline + float(net_cash) - float(corporate_costs_npv)
    return {"assets": parts, "pipeline_value": pipeline, "net_cash": float(net_cash),
            "corporate_costs_npv": float(corporate_costs_npv), "total": total}


def sotp(assets: list[dict], net_cash: float = 0.0, corporate_costs_npv: float = 0.0) -> float:
    """Calculate sum-of-the-parts valuation (USD millions).

    Args:
        assets: list of asset dicts, each with either 'rnpv' or full rNPV inputs,
            plus optional 'name', 'stage' and 'ownership'.
        net_cash: cash minus debt to add (USD millions).
        corporate_costs_npv: present value of unallocated corporate costs to subtract.
    """
    return sotp_breakdown(assets, net_cash, corporate_costs_npv)["total"]


# ---------------------------------------------------------------- comparables

def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return None
    return None if math.isnan(f) else f


def read_table(path: Union[str, Path]) -> list[dict]:
    """Read a CSV or Excel file into a list of dicts (blank cells become "")."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        import pandas as pd
        return pd.read_excel(path, dtype=object).fillna("").to_dict(orient="records")
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def load_deals_csv(path: Union[str, Path]) -> list[dict]:
    """Load a deal comps file (CSV or Excel).

    Columns (see data/templates/deal_comps_template.csv): acquirer_or_partner,
    target_or_licensor, deal_type, modality, indication, stage, date,
    transaction_value, upfront_portion, milestones_total, peak_sales_estimate.
    """
    return read_table(path)


def comparable_multiples(modality: str, indication: str,
                         deals: Optional[list] = None) -> dict[str, float]:
    """Comparable transaction multiples for a modality and indication.

    ``deals`` is a list of deal dicts (from ``load_deals_csv``) or DealSignal
    objects. A deal matches when its modality / indication contains the given
    text (case-insensitive; pass "" to skip a filter). Deals with no
    modality/indication field are kept, so a thesis's own deal signals work.

    Returns medians: median_total_value, median_upfront, upfront_to_total_ratio
    and, where a peak sales estimate is given, ev_per_peak_sales; plus 'n'.
    """
    if deals is None:
        raise ValueError("No deal data given. Pass deals= (e.g. load_deals_csv('my_comps.csv')) "
                         "or use the thesis's own deal signals.")
    rows = [d.model_dump() if hasattr(d, "model_dump") else dict(d) for d in deals]

    def match(row, key, want):
        if not want:
            return True
        have = str(row.get(key) or "")
        return (not have) or want.lower() in have.lower()

    picked = [r for r in rows if match(r, "modality", modality) and match(r, "indication", indication)]
    if not picked:
        raise ValueError(f"No deals match modality '{modality}' and indication '{indication}'")

    totals, upfronts, ratios, ev_peak = [], [], [], []
    for r in picked:
        total = _num(r.get("transaction_value"))
        up = _num(r.get("upfront_portion"))
        peak = _num(r.get("peak_sales_estimate"))
        if total is not None:
            totals.append(total)
        if up is not None:
            upfronts.append(up)
        if total and up is not None:
            ratios.append(up / total)
        if total and peak:
            ev_peak.append(total / peak)

    out: dict[str, float] = {"n": float(len(picked))}
    if totals:
        out["median_total_value"] = statistics.median(totals)
    if upfronts:
        out["median_upfront"] = statistics.median(upfronts)
    if ratios:
        out["upfront_to_total_ratio"] = statistics.median(ratios)
    if ev_peak:
        out["ev_per_peak_sales"] = statistics.median(ev_peak)
    return out


# --------------------------------------------------------------- freshness

def _launch_year(p: dict) -> int:
    for key in ("launch_year", "launch_date", "launched"):
        v = p.get(key)
        if v is None or v == "":
            continue
        if isinstance(v, (datetime, date)):
            return v.year
        s = str(v).strip()
        try:
            return int(float(s))
        except ValueError:
            return datetime.fromisoformat(s[:10]).year
    raise ValueError(f"Product '{p.get('name', '<unnamed>')}' needs launch_year or launch_date")


def freshness_index(products: list[dict], as_of_year: Optional[int] = None,
                    window_years: int = 5) -> float:
    """Freshness index: % of revenue from products launched in the last ``window_years``.

    Each product needs 'revenue' and 'launch_year' (or 'launch_date').
    Returns a percentage between 0 and 100.
    """
    if not products:
        raise ValueError("No products given")
    as_of = as_of_year or date.today().year
    total = fresh = 0.0
    for p in products:
        rev = _num(p.get("revenue"))
        if rev is None or rev < 0:
            raise ValueError(f"Product '{p.get('name', '<unnamed>')}' needs a non-negative revenue")
        total += rev
        if as_of - _launch_year(p) < window_years:
            fresh += rev
    if total == 0:
        return 0.0
    return 100.0 * fresh / total
