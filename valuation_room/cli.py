"""Command line interface: ``valuation-room``.

The LINEAGE argument is a lineage JSON file, or an asset ID (AST-NNNNNN) to fetch
from AssetProgramRegistry at ASSET_REGISTRY_URL.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .dataroom import build_dataroom
from .engine import scenarios, sensitivity, value
from .exports import BusClient, PublishRejected, dealsim_license_out, snapshot_envelope, sotp_csv, sotp_row
from .inputs import InputError  # noqa: F401  (subclass of ValueError, raised by the commands)
from .registry_client import AssetNotFound, RegistryUnavailable, fetch_lineage


def _parse_override(item: str) -> tuple[str, Any]:
    if "=" not in item:
        raise argparse.ArgumentTypeError(f"--override {item!r} must look like key=value")
    k, v = item.split("=", 1)
    v = v.strip()
    try:
        parsed: Any = json.loads(v)
    except json.JSONDecodeError:
        parsed = v  # e.g. phase_probabilities=phase_2=0.4;phase_3=0.6 or a date
    return k.strip(), parsed


def _load_lineage(arg: str) -> dict[str, Any]:
    p = Path(arg)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InputError(f"{arg} is not valid JSON: {exc}") from exc
        return data
    if re.match(r"^AST-\d{6}$", arg):
        return fetch_lineage(arg)
    raise InputError(f"{arg} is neither a lineage JSON file nor an asset ID")


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    ov = dict(_parse_override(o) for o in (args.override or []))
    if getattr(args, "as_of", None):
        ov["as_of"] = args.as_of
    return ov


def _emit(text: str, out: Optional[str]) -> None:
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _cmd_value(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    v = value(lineage, _overrides(args))
    if args.json:
        _emit(json.dumps(v.to_dict(), indent=2), args.output)
        return 0
    inp = v.inputs
    lines = [
        f"{inp.asset_name} ({inp.asset_id})  stage={inp.stage}  as_of={v.as_of}",
        f"rNPV            {v.rnpv:12,.1f}  USD m",
        f"NPV unrisked    {v.npv_unrisked:12,.1f}  USD m",
        f"Cumulative PoS  {v.cumulative_pos:12.3f}",
        f"Peak sales      {inp.peak_sales_millions:12,.0f}  USD m",
        f"Years to launch {inp.years_to_launch:12d}",
        f"Patent life     {inp.patent_life_years:12d}  years (expiry {inp.patent_expiry_year}"
        f"{', ESTIMATED' if inp.patent_expiry_estimated else ''})",
        f"COGS            {inp.cogs_pct:12.1%}",
        f"Discount rate   {inp.discount_rate:12.1%}",
        "", "Sources:",
    ]
    lines += [f"  {s['field']:<30} {s['source_system']:<22} {s['ref']}" for s in v.sources]
    if v.warnings:
        lines += ["", "Flags:"] + [f"  - {w}" for w in v.warnings]
    _emit("\n".join(lines), args.output)
    return 0


def _cmd_sensitivity(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    ov = _overrides(args)
    sens, scen = sensitivity(lineage, ov), scenarios(lineage, ov)
    if args.json:
        _emit(json.dumps({"sensitivity": sens, "scenarios": scen}, indent=2), args.output)
        return 0
    lines = [f"Base rNPV {sens['base_rnpv']:,.1f} USD m", "", f"{'Driver':<34}{'Low case':>24}{'High case':>24}"]
    for r in sens["rows"]:
        lo = f"{r['low']['label']}: {r['low']['rnpv']:,.1f}"
        hi = f"{r['high']['label']}: {r['high']['rnpv']:,.1f}"
        lines.append(f"{r['label']:<34}{lo:>24}{hi:>24}")
    lines += ["", "Scenarios (ILLUSTRATIVE multipliers): " + "  ".join(
        f"{k} {scen[k]['rnpv']:,.1f}" for k in ("bear", "base", "bull"))]
    _emit("\n".join(lines), args.output)
    return 0


def _cmd_export_sotp(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    row = sotp_row(value(lineage, _overrides(args)), ownership=args.ownership, include_rnpv=not args.no_rnpv,
                   extended=args.extended)
    _emit(sotp_csv([row], header=not args.no_header), args.output)
    return 0


def _cmd_export_dealsim(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    out = dealsim_license_out(lineage, _overrides(args), upfront_millions=args.upfront,
                              milestones_millions=args.milestones, royalty_pct=args.royalty_pct,
                              counterparty=args.counterparty, units=args.units)
    _emit(json.dumps(out, indent=2), args.output)
    return 0


def _cmd_snapshot(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    env = snapshot_envelope(value(lineage, _overrides(args)))
    if not args.publish:
        _emit(json.dumps(env, indent=2), args.output)
        return 0
    try:
        result = BusClient(args.bus_url, args.outbox).publish(env)
    except PublishRejected as exc:
        print(f"PharmaEventBus rejected the envelope: {exc}", file=sys.stderr)
        return 3
    print(json.dumps(result, indent=2))
    return 0


def _cmd_dataroom_build(args: argparse.Namespace) -> int:
    lineage = _load_lineage(args.lineage)
    res = build_dataroom(lineage, args.docs, args.out, thread=args.thread, overrides=_overrides(args),
                         make_zip=args.zip, force=args.force)
    idx = res.index
    req = [g for g in idx["gaps"] if g["level"] == "required"]
    print(f"Data room for {idx['asset_id']} written to {res.out_dir}")
    if res.zip_path:
        print(f"Zip: {res.zip_path}")
    print(f"{len(idx['documents'])} documents, {len(req)} required gap(s), "
          f"{len(idx['gaps']) - len(req)} recommended gap(s), {len(idx['unclassified'])} unclassified. See gaps.md.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="valuation-room", description="Registry-fed rNPV and diligence data room.")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("lineage", help="lineage JSON file, or an AST-NNNNNN id to fetch from the registry")
        sp.add_argument("--override", action="append", metavar="KEY=VALUE",
                        help="override an input, e.g. discount_rate=0.12 or cogs_per_dose=95000 (repeatable)")
        sp.add_argument("--as-of", help="valuation date YYYY-MM-DD (default today)")
        sp.add_argument("-o", "--output", help="write to this file instead of stdout")

    sp = sub.add_parser("value", help="rNPV with inputs and their sources")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_value)

    sp = sub.add_parser("sensitivity", help="tornado and bear/base/bull scenarios")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=_cmd_sensitivity)

    sp = sub.add_parser("export-sotp", help="thesis-generator sotp_assets CSV row")
    common(sp)
    sp.add_argument("--ownership", type=float, default=1.0)
    sp.add_argument("--no-rnpv", action="store_true", help="leave rnpv blank so thesis-generator recomputes")
    sp.add_argument("--no-header", action="store_true")
    sp.add_argument("--extended", action="store_true",
                    help="append ramp_years, cogs_pct, opex_pct, tax_rate (accepted by thesis-generator's importer)")
    sp.set_defaults(func=_cmd_export_sotp)

    sp = sub.add_parser("export-dealsim", help="DealStructureSimulator create_deal arguments (license-out)")
    common(sp)
    sp.add_argument("--upfront", type=float, help="upfront, USD millions")
    sp.add_argument("--milestones", type=float, help="total milestones, USD millions")
    sp.add_argument("--royalty-pct", type=float, help="royalty rate as a fraction, e.g. 0.12")
    sp.add_argument("--counterparty")
    sp.add_argument("--units", choices=["usd", "millions"], default="usd")
    sp.set_defaults(func=_cmd_export_dealsim)

    sp = sub.add_parser("snapshot", help="ValuationSnapshot MessageEnvelope (optionally publish)")
    common(sp)
    sp.add_argument("--publish", action="store_true", help="POST to PharmaEventBus (offline outbox fallback)")
    sp.add_argument("--bus-url", help="default PHARMA_BUS_URL or http://localhost:5102")
    sp.add_argument("--outbox", help="default VALUATION_OUTBOX or data/outbox.jsonl")
    sp.set_defaults(func=_cmd_snapshot)

    dr = sub.add_parser("dataroom", help="diligence data room")
    drs = dr.add_subparsers(dest="dataroom_command", required=True)
    sp = drs.add_parser("build", help="build the data room folder")
    sp.add_argument("lineage")
    sp.add_argument("--docs", required=True, help="documents directory (nothing outside it is read)")
    sp.add_argument("--out", required=True, help="output folder")
    sp.add_argument("--thread", help="DigitalThreadBuilder JSON export")
    sp.add_argument("--zip", action="store_true", help="also write <out>.zip")
    sp.add_argument("--force", action="store_true", help="replace a non-empty output folder")
    sp.add_argument("--override", action="append", metavar="KEY=VALUE")
    sp.add_argument("--as-of")
    sp.set_defaults(func=_cmd_dataroom_build)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, AssetNotFound, RegistryUnavailable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
