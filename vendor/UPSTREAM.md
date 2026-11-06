# Vendored upstream code

GitHub Actions cannot `pip install` Tim's private repos without a token, so the
two pieces of upstream code this repo reuses are pinned copies. At runtime
`valuation_room/_upstream.py` imports the installed upstream package first and
falls back to the copy only when it is not importable.

| Vendored file | Upstream repo | Upstream file | Commit | What is copied |
|---|---|---|---|---|
| `valuation_room/_vendor/thesisgen_valuation.py` | timjm25/thesis-generator | `thesisgen/valuation.py` | 9bab1ef | Whole module, byte-for-byte below a 7-line header (sha256 of upstream file: `ae400f31682f23829a1f038d49cd75dd3d399e6ea1fe5b5e07f84567e68b3668`) |
| `valuation_room/_vendor/pts_benchmarks.py` | timjm25/PTSPredictor | `ptspredict/model.py` | 8964589 | Constants `STAGES`, `MODALITIES`, `BENCHMARK_RATES`, `PTS_FLOOR`, `PTS_CEILING` and the upstream comment that labels the rates as directional planning assumptions |
| `tests/fixtures/sotp_assets_template.csv` | timjm25/thesis-generator | `data/templates/sotp_assets_template.csv` | 9bab1ef | Template, unchanged; the SOTP export test checks our header against it |

Runtime preference:

```python
try:
    import thesisgen.valuation as V           # installed upstream
except ImportError:
    from valuation_room._vendor import thesisgen_valuation as V
```

`Valuation.engine_source` and every `PTSPredictor` source ref record which copy was used.

## Refreshing a vendored copy

1. Check out the upstream repo at the new commit.
2. Replace the body of the vendored file (keep the header, update the commit sha).
3. Update the table above, then run `pytest -q`. `tests/test_parity.py` pins a
   hand-checked rNPV for thesis-generator's template example (1,757.06) and
   compares vendored and installed copies when the upstream package is present.
