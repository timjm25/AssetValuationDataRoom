"""Resolve upstream code: installed packages first, vendored copies as fallback.

``thesisgen.valuation`` (timjm25/thesis-generator) supplies ``rnpv_schedule``;
``ptspredict.model`` (timjm25/PTSPredictor) supplies the benchmark PTS tables.
Both are private repos, so CI uses the pinned copies in ``valuation_room/_vendor``.
"""
from __future__ import annotations

from types import ModuleType

try:  # pragma: no cover - depends on the environment
    import thesisgen.valuation as _tg_valuation  # type: ignore[import-not-found]

    VALUATION: ModuleType = _tg_valuation
    VALUATION_SOURCE = "thesisgen.valuation (installed)"
except ImportError:
    from ._vendor import thesisgen_valuation as _vendored_valuation

    VALUATION = _vendored_valuation
    VALUATION_SOURCE = "vendored thesis-generator@9bab1ef"

try:  # pragma: no cover - depends on the environment
    from ptspredict.model import (  # type: ignore[import-not-found]
        BENCHMARK_RATES,
        PTS_CEILING,
        PTS_FLOOR,
        STAGES as PTS_STAGES,
    )

    PTS_SOURCE = "ptspredict.model (installed)"
except ImportError:
    from ._vendor.pts_benchmarks import (
        BENCHMARK_RATES,
        PTS_CEILING,
        PTS_FLOOR,
        STAGES as PTS_STAGES,
    )

    PTS_SOURCE = "vendored PTSPredictor@8964589"

PHASE_ORDER: list[str] = list(VALUATION.PHASE_ORDER)
rnpv_schedule = VALUATION.rnpv_schedule
rnpv = VALUATION.rnpv
cumulative_pos = VALUATION.cumulative_pos

__all__ = [
    "VALUATION", "VALUATION_SOURCE", "PTS_SOURCE", "BENCHMARK_RATES", "PTS_FLOOR",
    "PTS_CEILING", "PTS_STAGES", "PHASE_ORDER", "rnpv_schedule", "rnpv", "cumulative_pos",
]
