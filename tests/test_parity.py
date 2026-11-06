"""Vendored thesis-generator rNPV parity against a hand-checked value."""
import math

import pytest

from valuation_room import _upstream
from valuation_room._vendor import pts_benchmarks
from valuation_room._vendor import thesisgen_valuation as vendored

# thesis-generator data/templates/sotp_assets_template.csv, row "EXAMPLE Lead asset"
TEMPLATE = dict(peak_sales_millions=1500, phase_probabilities={"phase_3": 0.6, "approval": 0.9},
                discount_rate=0.10, patent_life_years=12)
HAND_CHECKED = 1757.06  # USD m, see test_hand_calculation for the line-by-line build


def test_hand_calculation():
    """Spreadsheet-style build: launch year 3, 5-year S-curve, LoE after year 12, 60%/yr erosion x3."""
    share = lambda k: min(1.0, (1 - math.exp(-3.0 * k / 5)) / (1 - math.exp(-3.0)))  # noqa: E731
    margin = (1 - 0.15 - 0.25) * (1 - 0.21)  # COGS, opex, tax
    pos = 0.6 * 0.9
    pv = sum(1500 * share(t - 2) * margin * pos / 1.1 ** t for t in range(3, 13))
    pv += sum(1500 * share(10) * 0.4 ** n * margin * pos / 1.1 ** (12 + n) for n in (1, 2, 3))
    assert pv == pytest.approx(HAND_CHECKED, abs=0.005)


def test_vendored_rnpv_matches_hand_checked_template_value():
    assert vendored.rnpv(**TEMPLATE, years_to_launch=2) == pytest.approx(HAND_CHECKED, abs=0.005)


def test_runtime_engine_matches_vendored():
    assert _upstream.rnpv(**TEMPLATE, years_to_launch=2) == pytest.approx(
        vendored.rnpv(**TEMPLATE, years_to_launch=2))
    assert _upstream.PHASE_ORDER == vendored.PHASE_ORDER


def test_installed_upstream_matches_vendored_when_available():
    tg = pytest.importorskip("thesisgen.valuation")
    assert tg.rnpv(**TEMPLATE, years_to_launch=2) == pytest.approx(vendored.rnpv(**TEMPLATE, years_to_launch=2))


def test_installed_ptspredictor_matches_vendored_when_available():
    pm = pytest.importorskip("ptspredict.model")
    assert pm.BENCHMARK_RATES == pts_benchmarks.BENCHMARK_RATES
    assert (pm.PTS_FLOOR, pm.PTS_CEILING) == (pts_benchmarks.PTS_FLOOR, pts_benchmarks.PTS_CEILING)
