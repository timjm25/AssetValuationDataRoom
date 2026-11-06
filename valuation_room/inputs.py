"""Build rNPV inputs from an AssetProgramRegistry lineage record.

Every derived field records where it came from in ``RNPVInputs.sources`` as
``{field, source_system, ref}``. ``source_system`` is one of:

* ``AssetProgramRegistry``  - read from the lineage record (ref = JSON path)
* ``PTSPredictor``          - benchmark cumulative PTS table (vendored or installed)
* ``COGSBuilder``           - cost per dose vs price per dose passed in as overrides
* ``thesis-generator``      - a default of thesis-generator's ``rnpv_schedule``
* ``illustrative_default``  - a labelled planning assumption in this module
* ``user_override``         - an explicit override passed by the caller

Illustrative tables in this module are DIRECTIONAL PLANNING ASSUMPTIONS, not
facts. They exist so an early valuation can run end to end; replace them with
asset-specific diligence (override any field) before relying on the number.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping, Optional

from ._upstream import BENCHMARK_RATES, PTS_CEILING, PTS_FLOOR, PTS_SOURCE

ASSET_ID_RE = re.compile(r"^AST-\d{6}$")

REGISTRY_STAGES = (
    "discovery", "lead_optimization", "candidate", "ind_enabling", "ind_filed",
    "phase1", "phase2", "phase3", "filed", "approved", "terminated",
)

#: registry stage -> PTSPredictor stage (CONVENTIONS.md mapping table)
REGISTRY_TO_PTS_STAGE: dict[str, str] = {
    "discovery": "preclinical", "lead_optimization": "preclinical", "candidate": "preclinical",
    "ind_enabling": "ind_enabling", "ind_filed": "ind_enabling",
    "phase1": "phase1", "phase2": "phase2", "phase3": "phase3", "filed": "filed",
}

#: registry stage -> thesis-generator phase key (CONVENTIONS.md mapping table)
REGISTRY_TO_TG_PHASE: dict[str, str] = {
    "discovery": "preclinical", "lead_optimization": "preclinical", "candidate": "preclinical",
    "ind_enabling": "preclinical", "ind_filed": "ind", "phase1": "phase_1", "phase2": "phase_2",
    "phase3": "phase_3", "filed": "filing", "approved": "approval",
}

#: registry modality -> PTSPredictor modality (CONVENTIONS.md)
PTS_MODALITY: dict[str, str] = {
    "autologous_cell_therapy": "CAR-T",
    "gene_therapy_lvv": "LVV",
    "allogeneic_cell_therapy": "Allogeneic",
}

_CLINICAL_TAIL = ["phase_1", "phase_2", "phase_3", "approval"]

#: Remaining development steps (thesis-generator phase keys) from each registry stage.
#: Each key's probability is the CONDITIONAL chance of getting through that step.
REMAINING_PHASES: dict[str, list[str]] = {
    "discovery": ["preclinical", *_CLINICAL_TAIL],
    "lead_optimization": ["preclinical", *_CLINICAL_TAIL],
    "candidate": ["preclinical", *_CLINICAL_TAIL],
    "ind_enabling": ["preclinical", *_CLINICAL_TAIL],
    "ind_filed": ["ind", *_CLINICAL_TAIL],
    "phase1": list(_CLINICAL_TAIL),
    "phase2": ["phase_2", "phase_3", "approval"],
    "phase3": ["phase_3", "approval"],
    "filed": ["approval"],
    "approved": [],
}

# --------------------------------------------------------------------------
# ILLUSTRATIVE planning tables. Directional only, NOT sourced facts.
# --------------------------------------------------------------------------

#: ILLUSTRATIVE conditional phase transition probabilities for modalities with
#: no PTSPredictor benchmark. Styled on the shape of published industry phase
#: transition analyses (e.g. BIO / Informa / QLS clinical development success
#: rate reports): Phase 2 is the steepest gate and filing-to-approval is high.
#: Numbers are rounded planning assumptions, not quotations from any report.
ILLUSTRATIVE_PHASE_TRANSITIONS: dict[str, dict[str, float]] = {
    "small_molecule": {"phase_1": 0.52, "phase_2": 0.28, "phase_3": 0.57, "approval": 0.90},
    "monoclonal_antibody": {"phase_1": 0.55, "phase_2": 0.35, "phase_3": 0.65, "approval": 0.92},
    "other": {"phase_1": 0.52, "phase_2": 0.29, "phase_3": 0.58, "approval": 0.91},
}

#: ILLUSTRATIVE chance of reaching first-in-human from each preclinical stage.
ILLUSTRATIVE_PRECLINICAL_TO_PHASE1: dict[str, float] = {
    "discovery": 0.25, "lead_optimization": 0.35, "candidate": 0.50, "ind_enabling": 0.65,
}
#: ILLUSTRATIVE chance an open IND reaches first patient dosed.
ILLUSTRATIVE_IND_TO_PHASE1 = 0.90

#: ILLUSTRATIVE whole years from the START of each stage to commercial launch.
ILLUSTRATIVE_YEARS_TO_LAUNCH: dict[str, int] = {
    "discovery": 12, "lead_optimization": 10, "candidate": 9, "ind_enabling": 8,
    "ind_filed": 7, "phase1": 7, "phase2": 5, "phase3": 3, "filed": 1, "approved": 0,
}

#: ILLUSTRATIVE months from BLA/NDA submission to launch: FDA PDUFA review goal
#: (10 months standard, 6 months priority) plus the 60-day filing period.
REVIEW_MONTHS_STANDARD = 12
REVIEW_MONTHS_PRIORITY = 8

#: US orphan drug exclusivity (21 USC 360cc): 7 years from approval.
ORPHAN_EXCLUSIVITY_YEARS = 7
#: Patent term from priority/filing date used when no expiry is recorded (estimate).
PATENT_TERM_YEARS = 20
#: Fallback COGS share of revenue (also thesis-generator's default), flagged when used.
DEFAULT_COGS_PCT = 0.15
DEFAULT_DISCOUNT_RATE = 0.10

#: thesis-generator ``rnpv_schedule`` keyword defaults (commit 9bab1ef).
TG_DEFAULTS: dict[str, float] = {
    "opex_pct": 0.25, "tax_rate": 0.21, "ramp_years": 5, "erosion_per_year": 0.6, "erosion_years": 3,
}

ALLOWED_OVERRIDES = {
    "peak_sales_millions", "phase_probabilities", "pts", "discount_rate", "years_to_launch",
    "delay_years", "patent_life_years", "cogs_pct", "cogs_per_dose", "price_per_dose",
    "opex_pct", "tax_rate", "ramp_years", "erosion_per_year", "erosion_years", "phase_costs",
    "apply_orphan_exclusivity", "as_of", "stage",
}


class InputError(ValueError):
    """Raised when a lineage record cannot produce a required rNPV input."""


@dataclass
class Source:
    """Provenance of one rNPV input."""

    field: str
    source_system: str
    ref: str


@dataclass
class RNPVInputs:
    """Everything ``rnpv_schedule`` needs, plus provenance and flags."""

    asset_id: str
    asset_name: str
    modality: str
    stage: str
    as_of: date
    peak_sales_millions: float
    phase_probabilities: dict[str, float]
    discount_rate: float
    years_to_launch: int
    patent_life_years: int
    cogs_pct: float
    opex_pct: float
    tax_rate: float
    ramp_years: int
    erosion_per_year: float
    erosion_years: int
    delay_years: int = 0
    phase_costs: Optional[dict[str, float]] = None
    launch_date: Optional[date] = None
    patent_expiry_date: Optional[date] = None
    patent_expiry_estimated: bool = False
    orphan_designation: bool = False
    orphan_floor_applied: bool = False
    sources: list[Source] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def cumulative_pos(self) -> float:
        return math.prod(self.phase_probabilities.values()) if self.phase_probabilities else 1.0

    @property
    def patent_expiry_year(self) -> int:
        if self.patent_expiry_date is not None:
            return self.patent_expiry_date.year
        return self.as_of.year + self.patent_life_years

    @property
    def tg_phase(self) -> str:
        return REGISTRY_TO_TG_PHASE.get(self.stage, self.stage)

    def rnpv_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for thesis-generator ``rnpv_schedule``."""
        kw: dict[str, Any] = {
            "peak_sales_millions": self.peak_sales_millions,
            "phase_probabilities": dict(self.phase_probabilities),
            "discount_rate": self.discount_rate,
            "patent_life_years": self.patent_life_years,
            "years_to_launch": self.years_to_launch,
            "ramp_years": self.ramp_years,
            "cogs_pct": self.cogs_pct,
            "opex_pct": self.opex_pct,
            "tax_rate": self.tax_rate,
            "erosion_per_year": self.erosion_per_year,
            "erosion_years": self.erosion_years,
        }
        if self.phase_costs:
            kw["phase_costs"] = dict(self.phase_costs)
        return kw

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("as_of", "launch_date", "patent_expiry_date"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        d["cumulative_pos"] = self.cumulative_pos
        d["patent_expiry_year"] = self.patent_expiry_year
        d["tg_phase"] = self.tg_phase
        return d


# ------------------------------------------------------------------ helpers

def parse_date(value: Any) -> Optional[date]:
    """Parse an ISO 8601 date (or date-time) string; None for empty values."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise InputError(f"Not an ISO 8601 date: {value!r}") from exc


def years_between(start: date, end: date) -> float:
    return (end - start).days / 365.25


def add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def parse_phase_probabilities(value: Any) -> dict[str, float]:
    """Accept a dict or a thesis-generator style ``"k=v;k=v"`` string."""
    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, str):
        items = []
        for part in value.split(";"):
            if not part.strip():
                continue
            if "=" not in part:
                raise InputError(f"phase_probabilities entry {part!r} must look like phase_2=0.4")
            k, v = part.split("=", 1)
            items.append((k.strip(), v.strip()))
    else:
        raise InputError("phase_probabilities must be a dict or a 'k=v;k=v' string")
    out: dict[str, float] = {}
    for k, v in items:
        try:
            p = float(v)
        except (TypeError, ValueError) as exc:
            raise InputError(f"phase_probabilities[{k}] is not a number: {v!r}") from exc
        if not 0.0 <= p <= 1.0:
            raise InputError(f"phase_probabilities[{k}] must be between 0 and 1, got {p}")
        out[str(k)] = p
    return out


def scale_pos(phase_probabilities: Mapping[str, float], target_cumulative: float) -> dict[str, float]:
    """Rescale conditional probabilities so their product hits ``target_cumulative``.

    The adjustment is spread geometrically over the phases (each multiplied by
    the same factor). A phase that would exceed 1.0 is capped at 1.0 and the rest
    of the adjustment is re-spread over the remaining phases, so the target is
    met whenever it is <= 1.0.
    """
    probs = {k: float(v) for k, v in phase_probabilities.items()}
    if not probs:
        return probs
    if math.prod(probs.values()) <= 0:
        raise InputError("cannot rescale a cumulative PoS of zero")
    target = min(1.0, max(0.0, float(target_cumulative)))
    free = [k for k in probs if probs[k] < 1.0] or list(probs)
    for _ in range(len(probs) + 1):
        fixed = math.prod(probs[k] for k in probs if k not in free)
        current = math.prod(probs[k] for k in free)
        if not free or current <= 0:
            break
        factor = (target / fixed / current) ** (1.0 / len(free)) if fixed > 0 else 1.0
        scaled = {k: probs[k] * factor for k in free}
        over = [k for k, v in scaled.items() if v >= 1.0]
        if not over:
            probs.update(scaled)
            break
        for k in over:
            probs[k] = 1.0
        free = [k for k in free if k not in over]
    return probs


def _num(value: Any, name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{name} must be a number, got {value!r}") from exc


def _int(value: Any, name: str) -> int:
    f = _num(value, name)
    if f != int(f):
        raise InputError(f"{name} must be a whole number of years, got {value!r}")
    return int(f)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return bool(value)


# ------------------------------------------------------------- derivations

def _lead_indication(lineage: Mapping[str, Any]) -> Optional[dict]:
    inds = lineage.get("indications") or []
    leads = [i for i in inds if i.get("is_lead")]
    if leads:
        return leads[0]
    return inds[0] if len(inds) == 1 else None


def derive_phase_probabilities(modality: str, stage: str) -> tuple[dict[str, float], list[Source], list[str]]:
    """Conditional per-phase probabilities for the remaining steps from ``stage``.

    PTSPredictor modalities: ratio of consecutive CUMULATIVE benchmark rates, so
    the product of the returned values equals the benchmark cumulative rate at
    the current stage. Other modalities: the ILLUSTRATIVE tables above.
    """
    if stage not in REMAINING_PHASES:
        raise InputError(f"Stage {stage!r} has no forward development path to value")
    remaining = REMAINING_PHASES[stage]
    sources: list[Source] = []
    warnings: list[str] = []
    probs: dict[str, float] = {}
    if not remaining:
        sources.append(Source("phase_probabilities", "AssetProgramRegistry", "asset.stage=approved (PoS 1.0)"))
        return probs, sources, warnings

    pts_mod = PTS_MODALITY.get(modality)
    if pts_mod is not None:
        rates = BENCHMARK_RATES[pts_mod]
        pts_stage = REGISTRY_TO_PTS_STAGE[stage]
        tag = f"{PTS_SOURCE} BENCHMARK_RATES['{pts_mod}']"
        chain = {  # tg key -> (numerator stage, denominator stage or None)
            "preclinical": (pts_stage, "phase1"),
            "ind": (pts_stage, "phase1"),
            "phase_1": ("phase1", "phase2"),
            "phase_2": ("phase2", "phase3"),
            "phase_3": ("phase3", "filed"),
            "approval": ("filed", None),
        }
        for key in remaining:
            num, den = chain[key]
            if den is None:
                probs[key] = rates[num]
                ref = f"{tag}['{num}']"
            else:
                probs[key] = rates[num] / rates[den]
                ref = f"{tag}['{num}'] / ['{den}']"
            sources.append(Source(f"phase_probabilities.{key}", "PTSPredictor", ref))
        warnings.append(
            f"PoS from PTSPredictor {pts_mod} benchmark at stage '{pts_stage}' "
            f"(cumulative {rates[pts_stage]:.2f}); PTSPredictor labels these directional "
            "planning assumptions. Pass a driver-adjusted PTS with override pts=<value>."
        )
        return probs, sources, warnings

    group = modality if modality in ILLUSTRATIVE_PHASE_TRANSITIONS else "other"
    table = ILLUSTRATIVE_PHASE_TRANSITIONS[group]
    for key in remaining:
        if key == "preclinical":
            probs[key] = ILLUSTRATIVE_PRECLINICAL_TO_PHASE1[stage]
            ref = f"ILLUSTRATIVE_PRECLINICAL_TO_PHASE1['{stage}']"
        elif key == "ind":
            probs[key] = ILLUSTRATIVE_IND_TO_PHASE1
            ref = "ILLUSTRATIVE_IND_TO_PHASE1"
        else:
            probs[key] = table[key]
            ref = f"ILLUSTRATIVE_PHASE_TRANSITIONS['{group}']['{key}']"
        sources.append(Source(f"phase_probabilities.{key}", "illustrative_default", ref))
    warnings.append(
        f"PoS uses the ILLUSTRATIVE '{group}' phase transition table (directional planning "
        "assumption in the style of BIO/Informa phase transition analyses, not a sourced fact). "
        "Override phase_probabilities or pts with asset-specific estimates."
    )
    return probs, sources, warnings


def _registration_submission(lineage: Mapping[str, Any]) -> Optional[dict]:
    subs = [s for s in (lineage.get("submissions") or [])
            if s.get("kind") in ("bla", "nda") and s.get("status") not in ("rejected", "withdrawn")]
    if not subs:
        subs = [s for s in (lineage.get("submissions") or [])
                if s.get("kind") == "maa" and s.get("status") not in ("rejected", "withdrawn")]
    if not subs:
        return None
    return sorted(subs, key=lambda s: str(s.get("target_date") or s.get("submitted_date") or "9999"))[0]


def _granted_designation(lineage: Mapping[str, Any], designation: str, agency: str = "FDA") -> Optional[dict]:
    for s in lineage.get("submissions") or []:
        if (s.get("kind") == "designation" and s.get("designation") == designation
                and s.get("status") in ("granted", "approved") and s.get("agency", agency) == agency):
            return s
    return None


def build_inputs(lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]] = None) -> RNPVInputs:
    """Derive rNPV inputs from a lineage record; ``overrides`` win over derived values."""
    if not isinstance(lineage, Mapping):
        raise InputError("lineage must be a JSON object")
    ov = dict(overrides or {})
    unknown = sorted(set(ov) - ALLOWED_OVERRIDES)
    if unknown:
        raise InputError(f"Unknown override(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(ALLOWED_OVERRIDES))}")

    asset = lineage.get("asset") or {}
    asset_id = lineage.get("asset_id") or asset.get("asset_id")
    if not asset_id or not ASSET_ID_RE.match(str(asset_id)):
        raise InputError(f"lineage.asset_id must match AST-NNNNNN, got {asset_id!r}")
    sources: list[Source] = []
    warnings: list[str] = []
    base = f"{asset_id}"

    def src(fieldname: str, system: str, ref: str) -> None:
        sources.append(Source(fieldname, system, ref))

    as_of = parse_date(ov["as_of"]) if ov.get("as_of") else date.today()
    assert as_of is not None
    src("as_of", "user_override" if ov.get("as_of") else "illustrative_default",
        "override as_of" if ov.get("as_of") else "today's date")

    modality = str(asset.get("modality") or "other")
    if "stage" in ov:
        stage = str(ov["stage"])
        src("stage", "user_override", "override stage")
    else:
        stage = str(asset.get("stage") or "")
        src("stage", "AssetProgramRegistry", f"{base}/asset.stage")
    if stage not in REGISTRY_STAGES:
        raise InputError(f"asset.stage {stage!r} is not a registry stage ({', '.join(REGISTRY_STAGES)})")
    if stage == "terminated" or asset.get("status") == "terminated":
        raise InputError("Asset is terminated, so it has no forward rNPV. Override stage to value a restart scenario.")

    # -- peak sales
    if "peak_sales_millions" in ov:
        peak = _num(ov["peak_sales_millions"], "peak_sales_millions")
        src("peak_sales_millions", "user_override", "override peak_sales_millions")
    else:
        lead = _lead_indication(lineage)
        if lead is None or lead.get("peak_sales_millions") in (None, ""):
            where = f"indications[{lead.get('indication_id')}]" if lead else "indications[is_lead=true]"
            raise InputError(
                f"Missing field {where}.peak_sales_millions for {asset_id}: set it on the lead "
                "indication in AssetProgramRegistry or pass override peak_sales_millions=<USD m>."
            )
        peak = _num(lead["peak_sales_millions"], "peak_sales_millions")
        src("peak_sales_millions", "AssetProgramRegistry",
            f"{base}/indications/{lead.get('indication_id')}.peak_sales_millions")
    if peak < 0:
        raise InputError("peak_sales_millions must be >= 0")

    # -- probabilities
    if "phase_probabilities" in ov:
        probs = parse_phase_probabilities(ov["phase_probabilities"])
        src("phase_probabilities", "user_override", "override phase_probabilities")
    else:
        probs, psrc, pwarn = derive_phase_probabilities(modality, stage)
        sources.extend(psrc)
        warnings.extend(pwarn)
    if "pts" in ov:
        target = _num(ov["pts"], "pts")
        clamped = max(PTS_FLOOR, min(PTS_CEILING, target))
        if clamped != target:
            warnings.append(f"pts override {target} clamped to PTSPredictor bounds [{PTS_FLOOR}, {PTS_CEILING}]")
        probs = scale_pos(probs, clamped)
        src("phase_probabilities", "user_override",
            f"override pts={clamped:.4f} spread geometrically across {len(probs)} phases")

    # -- discount rate
    if "discount_rate" in ov:
        discount_rate = _num(ov["discount_rate"], "discount_rate")
        src("discount_rate", "user_override", "override discount_rate")
    else:
        discount_rate = DEFAULT_DISCOUNT_RATE
        src("discount_rate", "illustrative_default", "DEFAULT_DISCOUNT_RATE 0.10")

    # -- years to launch
    launch_date: Optional[date] = None
    if "years_to_launch" in ov:
        ytl = _int(ov["years_to_launch"], "years_to_launch")
        src("years_to_launch", "user_override", "override years_to_launch")
    else:
        reg = _registration_submission(lineage)
        if stage == "approved":
            ytl = 0
            src("years_to_launch", "AssetProgramRegistry", f"{base}/asset.stage=approved")
        elif reg is not None and (reg.get("decision_date") or reg.get("submitted_date") or reg.get("target_date")):
            if reg.get("decision_date"):
                launch_date = parse_date(reg["decision_date"])
                field_used = "decision_date"
                note = "approval decision date"
            else:
                field_used = "submitted_date" if reg.get("submitted_date") else "target_date"
                sub_date = parse_date(reg[field_used])
                assert sub_date is not None
                priority = _granted_designation(lineage, "priority_review") is not None
                months = REVIEW_MONTHS_PRIORITY if priority else REVIEW_MONTHS_STANDARD
                launch_date = sub_date + timedelta(days=round(months * 30.4375))
                note = (f"{field_used} + {months} months ILLUSTRATIVE review-to-launch "
                        f"({'priority' if priority else 'standard'} review)")
            assert launch_date is not None
            yrs = years_between(as_of, launch_date)
            if yrs < 0:
                warnings.append(f"Registration date {launch_date} is before as_of {as_of} but stage is {stage}; check the registry.")
            ytl = max(0, math.floor(yrs))
            src("years_to_launch", "AssetProgramRegistry",
                f"{base}/submissions/{reg.get('submission_id')}.{field_used} ({note})")
        else:
            ytl = ILLUSTRATIVE_YEARS_TO_LAUNCH[stage]
            src("years_to_launch", "illustrative_default", f"ILLUSTRATIVE_YEARS_TO_LAUNCH['{stage}']")
            warnings.append(
                f"No BLA/NDA submission date in the registry; years_to_launch={ytl} comes from the "
                "ILLUSTRATIVE stage duration table."
            )
    delay = _int(ov.get("delay_years", 0) or 0, "delay_years")
    if delay < 0:
        raise InputError("delay_years must be >= 0")
    if delay:
        ytl += delay
        if launch_date is not None:
            launch_date = add_years(launch_date, delay)
        src("delay_years", "user_override", f"override delay_years={delay} (ProgramDelayPredictor hook)")

    # -- patent life
    expiry: Optional[date] = None
    estimated = False
    if "patent_life_years" in ov:
        patent_life = _int(ov["patent_life_years"], "patent_life_years")
        src("patent_life_years", "user_override", "override patent_life_years")
    else:
        live = [r for r in (lineage.get("ip_refs") or []) if r.get("status") != "lapsed"]
        with_expiry = [r for r in live if r.get("expiry_date")]
        if with_expiry:
            ref = max(with_expiry, key=lambda r: parse_date(r["expiry_date"]) or date.min)
            expiry = parse_date(ref["expiry_date"])
            src("patent_life_years", "AssetProgramRegistry", f"{base}/ip_refs/{ref.get('ip_id')}.expiry_date")
        else:
            with_priority = [r for r in live if r.get("priority_date")]
            if not with_priority:
                raise InputError(
                    f"Missing field ip_refs[].expiry_date (or priority_date) for {asset_id}: add an IP "
                    "reference in AssetProgramRegistry or pass override patent_life_years=<years>."
                )
            ref = max(with_priority, key=lambda r: parse_date(r["priority_date"]) or date.min)
            prio = parse_date(ref["priority_date"])
            assert prio is not None
            expiry = add_years(prio, PATENT_TERM_YEARS)
            estimated = True
            src("patent_life_years", "AssetProgramRegistry",
                f"{base}/ip_refs/{ref.get('ip_id')}.priority_date + {PATENT_TERM_YEARS}y (ESTIMATED expiry)")
            warnings.append(f"Patent expiry ESTIMATED as priority date + {PATENT_TERM_YEARS} years ({expiry}); "
                            "no term extension or adjustment assumed.")
        assert expiry is not None
        patent_life = math.floor(years_between(as_of, expiry))
        if patent_life < 0:
            warnings.append(f"Latest patent expiry {expiry} is before as_of {as_of}; patent life set to 0.")
            patent_life = 0

    orphan = _granted_designation(lineage, "orphan") is not None
    floor_applied = False
    apply_orphan = _bool(ov.get("apply_orphan_exclusivity", True))
    if orphan and apply_orphan and "patent_life_years" not in ov:
        floor = ytl + ORPHAN_EXCLUSIVITY_YEARS
        if floor > patent_life:
            warnings.append(
                f"US orphan drug exclusivity floor applied: exclusivity extended from {patent_life} to "
                f"{floor} years (launch year + {ORPHAN_EXCLUSIVITY_YEARS} years). Indication-specific; confirm scope."
            )
            patent_life = floor
            floor_applied = True
            src("patent_life_years", "AssetProgramRegistry",
                f"{base}/submissions (FDA orphan designation granted) -> {ORPHAN_EXCLUSIVITY_YEARS}y ODE floor")
        else:
            warnings.append("FDA orphan designation granted; 7-year exclusivity floor not binding (patent runs longer).")

    # -- COGS
    if "cogs_pct" in ov:
        cogs = _num(ov["cogs_pct"], "cogs_pct")
        src("cogs_pct", "user_override", "override cogs_pct")
    elif ov.get("cogs_per_dose") not in (None, "") and ov.get("price_per_dose") not in (None, ""):
        cpd = _num(ov["cogs_per_dose"], "cogs_per_dose")
        ppd = _num(ov["price_per_dose"], "price_per_dose")
        if ppd <= 0:
            raise InputError("price_per_dose must be > 0")
        cogs = cpd / ppd
        src("cogs_pct", "COGSBuilder", f"cost_per_dose {cpd:,.0f} / price_per_dose {ppd:,.0f}")
    else:
        cogs = DEFAULT_COGS_PCT
        src("cogs_pct", "illustrative_default", "DEFAULT_COGS_PCT 0.15 (thesis-generator default)")
        msg = ("cogs_pct is the 0.15 default. Pass a COGSBuilder figure (cogs_per_dose and "
               "price_per_dose) or cogs_pct.")
        if modality in PTS_MODALITY:
            msg += " Cell and gene therapy COGS often run well above 15% of price."
        warnings.append(msg)
    if not 0.0 <= cogs <= 1.0:
        raise InputError(f"cogs_pct must be between 0 and 1, got {cogs}")

    other: dict[str, float] = {}
    for key, default in TG_DEFAULTS.items():
        if key in ov:
            other[key] = _num(ov[key], key)
            src(key, "user_override", f"override {key}")
        else:
            other[key] = default
            src(key, "thesis-generator", f"rnpv_schedule default {key}={default}")

    phase_costs = None
    if ov.get("phase_costs"):
        raw = ov["phase_costs"]
        phase_costs = parse_phase_probabilities_free(raw)
        src("phase_costs", "user_override", "override phase_costs")

    return RNPVInputs(
        asset_id=str(asset_id), asset_name=str(asset.get("name") or asset_id), modality=modality,
        stage=stage, as_of=as_of, peak_sales_millions=peak, phase_probabilities=probs,
        discount_rate=discount_rate, years_to_launch=ytl, patent_life_years=patent_life,
        cogs_pct=cogs, opex_pct=other["opex_pct"], tax_rate=other["tax_rate"],
        ramp_years=int(other["ramp_years"]), erosion_per_year=other["erosion_per_year"],
        erosion_years=int(other["erosion_years"]), delay_years=delay, phase_costs=phase_costs,
        launch_date=launch_date, patent_expiry_date=expiry, patent_expiry_estimated=estimated,
        orphan_designation=orphan, orphan_floor_applied=floor_applied, sources=sources, warnings=warnings,
    )


def parse_phase_probabilities_free(value: Any) -> dict[str, float]:
    """Parse ``phase_costs`` (dict or ``"k=v;k=v"``) without the 0..1 bound."""
    if isinstance(value, Mapping):
        return {str(k): _num(v, f"phase_costs[{k}]") for k, v in value.items()}
    if isinstance(value, str):
        out = {}
        for part in value.split(";"):
            if part.strip():
                if "=" not in part:
                    raise InputError(f"phase_costs entry {part!r} must look like phase_2=40")
                k, v = part.split("=", 1)
                out[k.strip()] = _num(v.strip(), f"phase_costs[{k.strip()}]")
        return out
    raise InputError("phase_costs must be a dict or a 'k=v;k=v' string")
