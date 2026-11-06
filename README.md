# AssetValuationDataRoom

AssetValuationDataRoom values one drug asset and assembles its diligence data room straight from the AssetProgramRegistry record, so nobody retypes valuation inputs into four tools. It reads the asset lineage (indications, submissions, IP, deals, partners), derives every rNPV input with a recorded source (registry field, PTSPredictor benchmark, COGSBuilder figure, or a clearly labelled illustrative default), values the asset with thesis-generator's own rNPV engine, runs a sensitivity tornado and bear/base/bull scenarios, and exports the result three ways: a ValuationSnapshot on PharmaEventBus, a thesis-generator SOTP row, and a DealStructureSimulator license-out deal. The data room builder files a documents folder (and an optional DigitalThreadBuilder export) into eight standard sections, checks a stage-aware required-document list, and writes a sha256 manifest, a readable index, a gaps list and the valuation summary. This is build #5 of the virtual pharma gap map ("Asset valuation + data room"); package `valuation_room`, port 5104.

All example data is fictional and marked EXAMPLE.

## What it links

| Existing repo | How | Files |
|---|---|---|
| AssetProgramRegistry | HTTP `GET /assets/<id>/lineage` (`ASSET_REGISTRY_URL`, default `http://localhost:5101`); data contract for offline use | `valuation_room/registry_client.py`, `examples/lineage_AST-000001.json`, `examples/lineage_AST-000002.json` |
| thesis-generator | Vendored code (rNPV engine) plus data contract (SOTP CSV) | `thesisgen/valuation.py` @ 9bab1ef vendored to `valuation_room/_vendor/thesisgen_valuation.py`; `data/templates/sotp_assets_template.csv` copied to `tests/fixtures/` |
| PTSPredictor | Vendored constants (benchmark cumulative PTS by modality and stage) | `ptspredict/model.py` @ 8964589 constants vendored to `valuation_room/_vendor/pts_benchmarks.py` |
| DealStructureSimulator | Data contract: component configs and `Model.create_deal` arguments | `dealsim/components.py` (`validate_component`), `dealsim/model.py` (`create_deal`) @ 2394f04 |
| PharmaEventBus | HTTP `POST /publish` of a `ValuationSnapshot` MessageEnvelope, offline JSONL outbox fallback | `valuation_room/exports.py` (`BusClient`) |
| DigitalThreadBuilder | Data contract: tolerant importer for `{"nodes":[{id,type,title,uri}],"edges":[...]}` | `valuation_room/dataroom.py` (`load_thread`), `examples/thread_AST-000002.json` |
| COGSBuilder | Override hook: `cogs_per_dose` and `price_per_dose` become `cogs_pct` | `valuation_room/inputs.py` |
| ProgramDelayPredictor | Override hook: `delay_years` pushes launch out | `valuation_room/inputs.py` |
| RegulatoryApprovalTimelinePredictor | Today via the registry's BLA/NDA `target_date`; live call is in "What's next" | `valuation_room/inputs.py` |
| CGT_CAGR | Today via the registry's `indications[].peak_sales_millions`; live market sizing is in "What's next" | lineage contract |
| IBThesisTool | Planned consumer of the same SOTP row and ValuationSnapshot (not yet verified against its importer) | `valuation_room/exports.py` |
| Contract-Clause-Extraction, SlideGenerator, PharmaPlatformShell | Not linked yet; see "What's next" | |

Upstream code is preferred at runtime: if `thesisgen.valuation` or `ptspredict.model` is importable it is used and the vendored copy is ignored. `vendor/UPSTREAM.md` records the pinned commits and how to refresh them.

## Quick start

```bash
pip install -e ".[dev]"
pytest -q

valuation-room value examples/lineage_AST-000002.json --as-of 2026-11-06
valuation-room sensitivity examples/lineage_AST-000002.json --as-of 2026-11-06
valuation-room dataroom build examples/lineage_AST-000002.json --docs examples/docs \
    --thread examples/thread_AST-000002.json --out dataroom_out/AST-000002 --zip --as-of 2026-11-06

valuation-room-api            # Flask on 127.0.0.1:5104 (HOST / PORT env)
```

## Worked example: EXAMPLE VR-201 autologous CAR-T (AST-000002)

A fictional CD19xCD22 autologous CAR-T in Phase 1 for relapsed/refractory large B-cell lymphoma. The lineage has a lead indication with peak sales of USD 900m, a planned BLA with target date 2030-06-30, granted RMAT and orphan designations, a PCT and a granted US patent, and a CDMO supply deal.

Valued as of 2026-11-06 (USD millions):

| Measure | Value |
|---|---|
| **rNPV** | **195.0** |
| NPV unrisked | 1,950.2 |
| Cumulative probability of success | 0.100 |
| Bear / base / bull rNPV | 95.6 / 195.0 / 304.2 |

Main inputs and where each came from:

| Input | Value | Source system | Reference |
|---|---|---|---|
| Peak sales | 900 | AssetProgramRegistry | `indications/IND-000003.peak_sales_millions` (lead indication) |
| Phase 1 to Phase 2 | 0.4545 | PTSPredictor | CAR-T cumulative rate phase1 0.10 / phase2 0.22 |
| Phase 2 to Phase 3 | 0.4231 | PTSPredictor | 0.22 / 0.52 |
| Phase 3 to filing | 0.6118 | PTSPredictor | 0.52 / 0.85 |
| Filing to approval | 0.85 | PTSPredictor | cumulative rate at "filed" |
| Years to launch | 4 | AssetProgramRegistry | `submissions/SUB-000035.target_date` 2030-06-30 + 12 months illustrative review-to-launch; launch mid-2031 falls in year 5 |
| Patent life | 18 years | AssetProgramRegistry | `ip_refs/IPR-000053.expiry_date` 2045-09-30 (latest live expiry) |
| COGS | 15% | illustrative_default | flagged: pass a COGSBuilder figure |
| Discount rate | 10% | illustrative_default | override with `discount_rate` |
| Opex, tax, ramp, erosion | 25%, 21%, 5 yrs, 60%/yr x3 | thesis-generator | `rnpv_schedule` defaults |

The four conditional probabilities multiply back to PTSPredictor's 0.10 cumulative rate for a Phase 1 CAR-T. The orphan designation's 7-year exclusivity floor is checked and reported as not binding because the patent runs longer. Sensitivity (largest swing first): peak sales +/-30% gives 136.5 to 253.5; PoS +/-20% relative gives 156.0 to 234.0; discount rate +/-2 pts gives 238.1 to 161.1; a 1 or 2 year launch delay gives 170.7 or 148.6.

A realistic autologous COGS changes the answer, which is why the default is flagged:

```bash
valuation-room value examples/lineage_AST-000002.json --as-of 2026-11-06 \
    --override cogs_per_dose=95000 --override price_per_dose=475000   # cogs_pct 0.20, source COGSBuilder
```

The example data room (`examples/docs/`) files 14 documents, generates the valuation summary, and reports two required gaps for a Phase 1 asset (preliminary non-GLP tox, CMC feasibility), one recommended gap (orphan designation letter), and one digital thread node (RD-002) whose file was not supplied.

## How the inputs are derived

| Field | Rule (first that applies) |
|---|---|
| `peak_sales_millions` | override; else lead indication's `peak_sales_millions`; else an error naming the missing field |
| `phase_probabilities` | override (dict or `k=v;k=v`); else for CAR-T, LVV and allogeneic modalities the ratio of consecutive PTSPredictor cumulative rates, keyed by thesis-generator phase names; else the ILLUSTRATIVE small molecule / mAb / other table. Optional `pts` rescales the chain to a driver-adjusted cumulative PTS (bounded by PTSPredictor's 0.02 floor and 0.95 ceiling) |
| `years_to_launch` | override; else BLA/NDA (then MAA) decision date, or submitted/target date plus 12 months (8 with a granted priority review), counted as whole years; else the ILLUSTRATIVE stage-duration table. Plus `delay_years` |
| `patent_life_years` | override; else latest non-lapsed `ip_refs[].expiry_date`; else latest `priority_date` + 20 years flagged ESTIMATED. A granted FDA orphan designation sets a floor of launch + 7 years (switch off with `apply_orphan_exclusivity=false`) |
| `cogs_pct` | override; else `cogs_per_dose / price_per_dose` (COGSBuilder); else 0.15 flagged |
| `discount_rate` | override; else 0.10 |
| opex, tax, ramp, erosion, `phase_costs` | override; else thesis-generator defaults |

Registry stage to thesis-generator phase key follows CONVENTIONS.md (for example `phase1` to `phase_1`, `ind_filed` to `ind`).

### Illustrative assumptions (not facts)

Everything below is a directional planning assumption, labelled ILLUSTRATIVE in code, sources and warnings, and overridable:

* PTSPredictor's benchmark rates (its own code calls them directional planning assumptions).
* `ILLUSTRATIVE_PHASE_TRANSITIONS`: conditional phase success for small molecule (0.52 / 0.28 / 0.57 / 0.90), mAb (0.55 / 0.35 / 0.65 / 0.92) and other (0.52 / 0.29 / 0.58 / 0.91), shaped like published BIO / Informa phase-transition analyses but rounded and not quoted from any report; preclinical-to-Phase 1 by stage (0.25 to 0.65).
* Stage-to-launch years, 12 / 8 month review-to-launch, the 0.15 COGS default and 10% discount rate.
* Scenario multipliers (bear: peak x0.7, PoS x0.8, +1 year; bull: peak x1.3, PoS x1.2).
* License-out placeholders when no terms exist: upfront 25% of rNPV, milestones 3x upfront, royalty 12%, and the split of milestones across phases.

## CLI reference

`LINEAGE` is a lineage JSON file, or an asset ID (`AST-000002`) to fetch from the registry. Every value-type command takes `--override KEY=VALUE` (repeatable), `--as-of YYYY-MM-DD` and `-o FILE`.

| Command | Output |
|---|---|
| `valuation-room value LINEAGE [--json]` | rNPV, inputs, sources, flags |
| `valuation-room sensitivity LINEAGE [--json]` | tornado and bear/base/bull |
| `valuation-room export-sotp LINEAGE [--ownership 0.5] [--no-rnpv] [--extended]` | thesis-generator SOTP CSV row (template columns; `--extended` adds ramp_years, cogs_pct, opex_pct, tax_rate, which thesis-generator's importer accepts) |
| `valuation-room export-dealsim LINEAGE [--upfront M] [--milestones M] [--royalty-pct 0.1] [--counterparty X] [--units usd\|millions]` | `create_deal` argument dict |
| `valuation-room snapshot LINEAGE [--publish] [--bus-url URL] [--outbox FILE]` | ValuationSnapshot envelope; `--publish` posts it, queuing offline if the bus is down |
| `valuation-room dataroom build LINEAGE --docs DIR --out DIR [--thread FILE] [--zip] [--force]` | data room folder and optional zip |

Override keys: `peak_sales_millions, phase_probabilities, pts, discount_rate, years_to_launch, delay_years, patent_life_years, cogs_pct, cogs_per_dose, price_per_dose, opex_pct, tax_rate, ramp_years, erosion_per_year, erosion_years, phase_costs, apply_orphan_exclusivity, as_of, stage`.

## API reference (port 5104)

| Method and path | Body / query | Response |
|---|---|---|
| `GET /healthz` | | `{"status":"ok"}` |
| `GET /openapi.json` | | minimal served contract |
| `POST /value[?sensitivity=1]` | `{"lineage": {...}, "overrides": {...}}` or a bare lineage | valuation JSON (rnpv, npv_unrisked, cumulative_pos, inputs, sources, warnings, schedule) |
| `POST /sensitivity` | same | tornado and scenarios |
| `GET /assets/<asset_id>/value[?override=k=v]` | | lineage fetched from `ASSET_REGISTRY_URL`; 404 if the registry has no such asset; 502 with a clear message if the registry is down |
| `POST /dataroom/gaps` | `{"lineage": {...}, "documents": ["file names"]}` | stage-aware checklist, gaps, unclassified names |

Input errors (for example a missing peak sales figure) return 400 with the field named.

## Data model

**Sources.** Every derived field adds `{field, source_system, ref}`; `source_system` is one of AssetProgramRegistry, PTSPredictor, COGSBuilder, thesis-generator, illustrative_default, user_override.

**ValuationSnapshot** (exactly per CONVENTIONS.md): `asset_id, as_of, currency "USD", units "millions", rnpv, npv_unrisked, cumulative_pos, peak_sales_millions, discount_rate, years_to_launch, patent_expiry_year, assumptions, sources, method "rNPV"`, wrapped in MessageEnvelope 1.0.0 with `source_agent "asset-valuation"`, `correlation_id = asset_id`, `gxp_relevant false`. Bus URL from `PHARMA_BUS_URL`; outbox from `VALUATION_OUTBOX` (default `data/outbox.jsonl`). A 422 from the bus raises and is not queued.

**DealStructureSimulator deal.** Upfront at year 0; one milestone per remaining phase transition with probability equal to the probability of reaching that phase; a royalty with `sales_low / likely / high` from bear / base / bull peak sales, `probability` equal to cumulative PoS, `years` equal to exclusive years on market, plus `start_year` (launch year). Amounts default to whole USD to match DealStructureSimulator's seed data. Terms come from a registry `license_out` deal when one exists, else from the caller, else illustrative placeholders (each recorded in `term_sources`).

**Data room layout.**

```
<out>/
  01_Corporate_and_Summary/ 02_IP/ 03_Discovery_and_Preclinical/ 04_CMC_and_Manufacturing/
  05_Clinical/ 06_Regulatory/ 07_Commercial_and_Valuation/ 08_Contracts_and_Partners/
  99_Unclassified/                     (only if something could not be filed)
  07_Commercial_and_Valuation/valuation_summary.md, valuation.json   (generated)
  index.json   documents[]: path, original_path, section, doc_type, sha256, size, source, asset_id, thread_nodes
  index.html   readable index
  gaps.md      missing required / recommended documents, unclassified files, thread references with no file
```

Documents are filed by file-name rules, overridden by an optional sidecar `<file>.meta.json` (`{"section": 4, "doc_type": "cmc_feasibility", "asset_id": "AST-000002"}`); a sidecar naming another asset excludes the file. Digital thread nodes annotate matching files, file otherwise-unclassified ones by node type or ID prefix (RD to Discovery, MFG to CMC, CLN to Clinical, REG to Regulatory), list external URIs as references, and flag references to files that were not supplied.

Required documents by stage: candidate needs TPP, target validation, in vitro pharmacology, preliminary tox, provisional patent and CMC feasibility; IND-enabling adds GLP tox, CMC Module 3 draft and pre-IND minutes; IND filed adds the IND; clinical adds protocol and investigator's brochure, and the CSR once a clinical study is complete or from Phase 2. Granted designations and signed deals in the lineage add recommended letters and agreements.

**Safety.** Only regular files inside `--docs` are read. Symlinked files and folders are skipped; thread URIs that resolve outside the folder (`..`, absolute paths, symlinks) are rejected and reported; the output folder may not sit inside the documents folder.

## What's next

* Live calls to COGSBuilder (cost per dose) and RegulatoryApprovalTimelinePredictor (approval date distribution) instead of overrides and registry dates; ProgramDelayPredictor's expected delay fed into `delay_years`; CGT_CAGR market sizing into peak sales.
* PTSPredictor driver adjustments read automatically (today pass `pts=`).
* US biologics 12-year data exclusivity as a second exclusivity floor.
* SlideGenerator deck export of the valuation summary, tornado and data room index.
* Contract-Clause-Extraction run over section 8 documents to pull deal terms into the DealStructureSimulator export.
* Azure Blob storage for data rooms, and access-controlled sharing through PharmaPlatformShell (today the room is a local folder or zip with no access control).
* DealStructureSimulator: honour the royalty `start_year` this tool already emits.
