"""Vendored PTSPredictor benchmark constants.

VENDORED COPY - do not edit by hand.
Source: timjm25/PTSPredictor, file ptspredict/model.py, commit 8964589
(constants STAGES, MODALITIES, BENCHMARK_RATES, PTS_FLOOR, PTS_CEILING copied
verbatim, including the upstream comment). At runtime valuation_room prefers
the installed ``ptspredict.model`` when it is importable. See vendor/UPSTREAM.md.
"""

STAGES = ("preclinical", "ind_enabling", "phase1", "phase2", "phase3", "filed")

MODALITIES = ("CAR-T", "LVV", "Allogeneic")

# Illustrative benchmark cumulative probability of eventual approval, by
# modality and current clinical stage. Modeled on the well-documented
# industry pattern that (a) probability of success rises sharply at each
# later phase gate, and (b) cell & gene therapy modalities historically
# succeed at lower rates than small-molecule biopharma, largely driven by
# manufacturing/CMC complexity and novel mechanisms. These are directional
# planning assumptions, not a substitute for asset-specific diligence.
BENCHMARK_RATES = {
    "CAR-T": {
        "preclinical": 0.04,
        "ind_enabling": 0.06,
        "phase1": 0.10,
        "phase2": 0.22,
        "phase3": 0.52,
        "filed": 0.85,
    },
    "LVV": {
        "preclinical": 0.05,
        "ind_enabling": 0.08,
        "phase1": 0.13,
        "phase2": 0.27,
        "phase3": 0.58,
        "filed": 0.88,
    },
    "Allogeneic": {
        "preclinical": 0.03,
        "ind_enabling": 0.05,
        "phase1": 0.08,
        "phase2": 0.17,
        "phase3": 0.45,
        "filed": 0.80,
    },
}

PTS_FLOOR = 0.02
PTS_CEILING = 0.95
