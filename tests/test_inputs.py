"""rNPV input derivation from lineage records."""
import math

import pytest

from valuation_room._upstream import BENCHMARK_RATES
from valuation_room.inputs import (ILLUSTRATIVE_PHASE_TRANSITIONS, InputError, build_inputs,
                                   derive_phase_probabilities, parse_phase_probabilities)


def _src(inputs, field):
    return [s for s in inputs.sources if s.field == field]


def test_cart_conditional_probabilities_multiply_back_to_cumulative(cart, ov):
    inp = build_inputs(cart, ov)
    assert list(inp.phase_probabilities) == ["phase_1", "phase_2", "phase_3", "approval"]
    assert inp.cumulative_pos == pytest.approx(BENCHMARK_RATES["CAR-T"]["phase1"])
    assert inp.phase_probabilities["phase_1"] == pytest.approx(0.10 / 0.22)
    assert inp.phase_probabilities["approval"] == pytest.approx(0.85)
    assert all(s.source_system == "PTSPredictor" for s in inp.sources if s.field.startswith("phase_probabilities."))


@pytest.mark.parametrize("modality,pts_mod", [("autologous_cell_therapy", "CAR-T"),
                                              ("gene_therapy_lvv", "LVV"),
                                              ("allogeneic_cell_therapy", "Allogeneic")])
@pytest.mark.parametrize("stage,pts_stage", [("candidate", "preclinical"), ("ind_enabling", "ind_enabling"),
                                             ("ind_filed", "ind_enabling"), ("phase2", "phase2"),
                                             ("phase3", "phase3"), ("filed", "filed")])
def test_benchmark_product_equals_cumulative_rate_every_stage(modality, pts_mod, stage, pts_stage):
    probs, _, _ = derive_phase_probabilities(modality, stage)
    assert math.prod(probs.values()) == pytest.approx(BENCHMARK_RATES[pts_mod][pts_stage])
    assert all(0 < p <= 1 for p in probs.values())


def test_small_molecule_uses_labelled_illustrative_table(small_molecule, ov):
    inp = build_inputs(small_molecule, ov)
    table = ILLUSTRATIVE_PHASE_TRANSITIONS["small_molecule"]
    assert inp.phase_probabilities == {"phase_2": table["phase_2"], "phase_3": table["phase_3"],
                                       "approval": table["approval"]}
    assert all(s.source_system == "illustrative_default" for s in _src(inp, "phase_probabilities.phase_2"))
    assert any("ILLUSTRATIVE" in w for w in inp.warnings)


def test_missing_peak_sales_names_the_field(cart, ov):
    cart["indications"][0]["peak_sales_millions"] = None
    with pytest.raises(InputError, match=r"indications\[IND-000003\]\.peak_sales_millions"):
        build_inputs(cart, ov)
    # an explicit override unblocks it
    assert build_inputs(cart, {**ov, "peak_sales_millions": 500}).peak_sales_millions == 500


def test_overrides_win_and_are_sourced(cart, ov):
    inp = build_inputs(cart, {**ov, "peak_sales_millions": 1200, "discount_rate": 0.12,
                              "phase_probabilities": "phase_1=0.5;approval=0.9", "years_to_launch": 6,
                              "patent_life_years": 15, "cogs_pct": 0.3})
    assert (inp.peak_sales_millions, inp.discount_rate, inp.years_to_launch,
            inp.patent_life_years, inp.cogs_pct) == (1200, 0.12, 6, 15, 0.3)
    assert inp.phase_probabilities == {"phase_1": 0.5, "approval": 0.9}
    for f in ("peak_sales_millions", "discount_rate", "phase_probabilities", "years_to_launch",
              "patent_life_years", "cogs_pct"):
        assert _src(inp, f)[-1].source_system == "user_override"


def test_unknown_override_rejected(cart, ov):
    with pytest.raises(InputError, match="Unknown override"):
        build_inputs(cart, {**ov, "peak_salez": 1})


def test_pts_override_rescales_to_target(cart, ov):
    inp = build_inputs(cart, {**ov, "pts": 0.15})
    assert inp.cumulative_pos == pytest.approx(0.15)
    capped = build_inputs(cart, {**ov, "pts": 0.99})
    assert capped.cumulative_pos == pytest.approx(0.95)  # PTSPredictor ceiling
    assert any("clamped" in w for w in capped.warnings)


def test_patent_life_from_latest_expiry(cart, ov):
    inp = build_inputs(cart, ov)
    assert inp.patent_expiry_date.isoformat() == "2045-09-30"
    assert inp.patent_life_years == 18 and not inp.patent_expiry_estimated
    assert _src(inp, "patent_life_years")[0].ref.endswith("IPR-000053.expiry_date")


def test_patent_life_estimated_from_priority_date(small_molecule, ov):
    inp = build_inputs(small_molecule, ov)
    assert inp.patent_expiry_estimated
    assert inp.patent_expiry_date.isoformat() == "2042-06-01"
    assert inp.patent_life_years == 15
    assert "ESTIMATED" in _src(inp, "patent_life_years")[0].ref


def test_lapsed_ip_ignored_and_no_ip_is_an_error(cart, ov):
    for r in cart["ip_refs"]:
        r["status"] = "lapsed"
    with pytest.raises(InputError, match=r"ip_refs\[\]\.expiry_date"):
        build_inputs(cart, ov)


def test_years_to_launch_from_bla_target_date_plus_review(cart, ov):
    inp = build_inputs(cart, ov)
    # BLA target 2030-06-30 + 12 months -> launch mid-2031 -> 4.6 years -> first revenue year 5
    assert inp.launch_date.isoformat().startswith("2031-06")
    assert inp.years_to_launch == 4
    assert "SUB-000035.target_date" in _src(inp, "years_to_launch")[0].ref


def test_years_to_launch_falls_back_to_illustrative_table_and_delay_adds(small_molecule, ov):
    inp = build_inputs(small_molecule, ov)
    assert inp.years_to_launch == 5
    assert _src(inp, "years_to_launch")[0].source_system == "illustrative_default"
    delayed = build_inputs(small_molecule, {**ov, "delay_years": 2})
    assert delayed.years_to_launch == 7
    assert _src(delayed, "delay_years")[0].source_system == "user_override"


def test_orphan_floor_applies_only_when_binding(cart, ov):
    assert not build_inputs(cart, ov).orphan_floor_applied
    cart["ip_refs"] = [{"ip_id": "IPR-000099", "kind": "granted", "status": "granted",
                        "priority_date": "2010-01-01", "expiry_date": "2032-01-01"}]
    inp = build_inputs(cart, ov)
    assert inp.orphan_floor_applied and inp.patent_life_years == inp.years_to_launch + 7
    off = build_inputs(cart, {**ov, "apply_orphan_exclusivity": False})
    assert not off.orphan_floor_applied and off.patent_life_years == 5


def test_cogs_from_cogsbuilder_figures_else_flagged_default(cart, ov):
    inp = build_inputs(cart, {**ov, "cogs_per_dose": 95000, "price_per_dose": 475000})
    assert inp.cogs_pct == pytest.approx(0.20)
    assert _src(inp, "cogs_pct")[0].source_system == "COGSBuilder"
    default = build_inputs(cart, ov)
    assert default.cogs_pct == 0.15 and any("cogs_pct is the 0.15 default" in w for w in default.warnings)


def test_terminated_and_bad_ids_rejected(cart, ov):
    cart["asset"]["stage"] = "terminated"
    with pytest.raises(InputError, match="terminated"):
        build_inputs(cart, ov)
    with pytest.raises(InputError, match="AST-NNNNNN"):
        build_inputs({"asset_id": "X-1"}, ov)


def test_parse_phase_probabilities_validates():
    assert parse_phase_probabilities("phase_2=0.4; approval=0.9") == {"phase_2": 0.4, "approval": 0.9}
    with pytest.raises(InputError):
        parse_phase_probabilities("phase_2=1.4")
