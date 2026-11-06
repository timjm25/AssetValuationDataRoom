"""Valuation, sensitivity and scenarios."""
import pytest

from valuation_room._vendor import thesisgen_valuation as vendored
from valuation_room.engine import scenarios, sensitivity, value
from valuation_room.inputs import InputError


def test_cart_value_matches_vendored_rnpv_with_same_inputs(cart, ov):
    v = value(cart, ov)
    kw = v.inputs.rnpv_kwargs()
    assert v.rnpv == pytest.approx(vendored.rnpv(**kw))
    assert v.rnpv == pytest.approx(195.02, abs=0.01)
    assert v.cumulative_pos == pytest.approx(0.10)
    # commercial cash flows are linear in PoS when there are no phase costs
    assert v.npv_unrisked == pytest.approx(v.rnpv / 0.10)
    assert sum(r["present_value"] for r in v.schedule) == pytest.approx(v.rnpv, abs=1e-3)


def test_to_dict_carries_sources_and_warnings(cart, ov):
    d = value(cart, ov).to_dict()
    assert d["method"] == "rNPV" and d["units"] == "millions"
    fields = {s["field"] for s in d["sources"]}
    assert {"peak_sales_millions", "years_to_launch", "patent_life_years", "cogs_pct", "discount_rate"} <= fields
    assert d["warnings"]


def test_phase_costs_reduce_value(cart, ov):
    base = value(cart, ov).rnpv
    with_costs = value(cart, {**ov, "phase_costs": {"phase_1": 20, "phase_2": 60}}).rnpv
    assert with_costs < base


def test_sensitivity_signs_and_ordering(cart, ov):
    s = sensitivity(cart, ov)
    rows = {r["driver"]: r for r in s["rows"]}
    swings = [r["swing"] for r in s["rows"]]
    assert swings == sorted(swings, reverse=True)
    assert rows["peak_sales_millions"]["high"]["delta"] > 0 > rows["peak_sales_millions"]["low"]["delta"]
    assert rows["discount_rate"]["high"]["delta"] < 0 < rows["discount_rate"]["low"]["delta"]
    assert rows["cumulative_pos"]["high"]["delta"] > 0 > rows["cumulative_pos"]["low"]["delta"]
    assert rows["launch_delay"]["low"]["delta"] < rows["launch_delay"]["high"]["delta"] < 0
    # peak sales and PoS are linear here: +/-30% and +/-20% of base
    assert rows["peak_sales_millions"]["high"]["delta"] == pytest.approx(0.30 * s["base_rnpv"], rel=1e-4)
    assert rows["cumulative_pos"]["high"]["delta"] == pytest.approx(0.20 * s["base_rnpv"], rel=1e-4)


def test_scenarios_ordered(cart, ov):
    sc = scenarios(cart, ov)
    assert sc["bear"]["rnpv"] < sc["base"]["rnpv"] < sc["bull"]["rnpv"]
    assert sc["base"]["rnpv"] == pytest.approx(value(cart, ov).rnpv, abs=1e-3)
    assert sc["bear"]["years_to_launch"] == sc["base"]["years_to_launch"] + 1


def test_approved_asset_has_pos_one(small_molecule, ov):
    small_molecule["asset"]["stage"] = "approved"
    v = value(small_molecule, ov)
    assert v.cumulative_pos == 1.0 and v.inputs.years_to_launch == 0
    assert v.rnpv == pytest.approx(v.npv_unrisked)


def test_invalid_engine_input_is_input_error(cart, ov):
    with pytest.raises(InputError):
        value(cart, {**ov, "discount_rate": -1.5})
