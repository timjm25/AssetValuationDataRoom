"""Diligence data room builder for one asset.

Inputs: the asset lineage record, a documents directory and, optionally, a
DigitalThreadBuilder JSON export. Output: a folder (and optional zip) with the
documents filed into eight standard sections, ``index.json`` (sha256, size,
section, source, asset_id per file), ``index.html``, ``gaps.md`` and a valuation
summary.

Safety: only regular files that live inside the given documents directory are
ever read or copied. Symlinks are skipped, and digital-thread URIs that resolve
outside the documents directory (``..``, absolute paths, symlinks) are rejected.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from .inputs import REGISTRY_STAGES, InputError

SECTIONS: dict[str, str] = {
    "01": "Corporate & Summary",
    "02": "IP",
    "03": "Discovery & Preclinical",
    "04": "CMC & Manufacturing",
    "05": "Clinical",
    "06": "Regulatory",
    "07": "Commercial & Valuation",
    "08": "Contracts & Partners",
}
UNCLASSIFIED = "99"
SECTION_FOLDERS = {k: f"{k}_{v.replace(' & ', '_and_').replace(' ', '_')}" for k, v in SECTIONS.items()}
SECTION_FOLDERS[UNCLASSIFIED] = "99_Unclassified"

# (doc_type, section, label, pattern on the normalized name). Order matters: first match wins.
DOC_TYPE_RULES: list[tuple[str, str, str, str]] = [
    ("pre_ind_minutes", "06", "Pre-IND meeting minutes", r"\bpre ?ind\b"),
    ("ind_application", "06", "IND application", r"\bind (application|submission|cover|filing)|form ?1571"),
    ("designation_letter", "06", "Designation letter",
     r"\b(rmat|orphan|breakthrough|fast ?track|priority review|odd)\b.*\b(designation|letter|grant)|\bdesignation\b"),
    ("cmc_module3", "04", "CMC Module 3 (draft)", r"\bmodule ?3\b|\bctd ?3\b|\bm3\b"),
    ("cmc_feasibility", "04", "CMC feasibility assessment",
     r"\b(cmc|manufacturing|process|tech(nology)? transfer) feasibility\b"),
    ("batch_record", "04", "Batch record / CoA", r"\bbatch record\b|\bbmr\b|\bcoa\b|certificate of analysis"),
    ("glp_tox", "03", "GLP toxicology report", r"\bglp\b.*\btox|\btox.*\bglp\b"),
    ("preliminary_tox", "03", "Preliminary (non-GLP) toxicology",
     r"\b(prelim\w*|dose range\w*|drf|non ?glp|exploratory)\b.*\btox|\bmtd\b"),
    ("target_validation", "03", "Target validation", r"\btarget validation\b"),
    ("in_vitro_pharmacology", "03", "In vitro pharmacology", r"\bin vitro\b|\bpharmacology\b|\bcytotox\w*|\bpotency\b"),
    ("tpp", "01", "Target product profile", r"\btarget product profile\b|\btpp\b"),
    ("csr", "05", "Clinical study report", r"\bclinical study report\b|\bcsr\b"),
    ("investigator_brochure", "05", "Investigator's brochure", r"\binvestigator'?s? brochure\b|\bib\b"),
    ("protocol", "05", "Clinical protocol", r"\bprotocol\b"),
    ("provisional_patent", "02", "Provisional patent application", r"\bprovisional\b"),
    ("patent", "02", "Patent / IP document",
     r"\bpatent\b|\bpct\b|\bfto\b|freedom to operate|invention disclosure"),
    ("contract", "08", "Contract / agreement",
     r"\bagreement\b|\bcontract\b|\bmsa\b|\bterm sheet\b|\bcda\b|\bconfidentiality\b|\blicen[cs]e\b"),
    ("commercial", "07", "Commercial / market analysis",
     r"\bmarket\b|\bforecast\b|\bpricing\b|\bvaluation\b|\brnpv\b|\bcommercial\b"),
    ("corporate", "01", "Corporate overview", r"\bsummary\b|\boverview\b|\bteaser\b|\bexecutive\b|\bcorporate\b"),
]
DOC_TYPE_SECTION = {t: s for t, s, _, _ in DOC_TYPE_RULES}
DOC_TYPE_SECTION["valuation_summary"] = "07"
DOC_TYPE_LABEL = {t: lbl for t, _, lbl, _ in DOC_TYPE_RULES}
DOC_TYPE_LABEL["valuation_summary"] = "Valuation summary (generated)"

_STAGE_INDEX = {s: i for i, s in enumerate(REGISTRY_STAGES)}

# Digital thread: node id prefix / type keyword -> section
THREAD_PREFIX_SECTION = {
    "RD": "03", "PRE": "03", "PC": "03", "MFG": "04", "CMC": "04", "BAT": "04", "BATCH": "04",
    "QC": "04", "CLN": "05", "CLIN": "05", "REG": "06", "IP": "02", "COM": "07", "CTR": "08", "LEG": "08",
}
THREAD_TYPE_SECTION = [
    (r"clinic|trial|patient", "05"), (r"regulat|submission|filing|designation", "06"),
    (r"batch|process|manufactur|material|deviation|release|cmc|qc", "04"),
    (r"patent|\bip\b", "02"), (r"contract|agreement", "08"),
    (r"study|assay|experiment|research|discovery|preclinical|tox", "03"),
]


def normalize_name(name: str) -> str:
    """Lower-case and turn separators into spaces so word boundaries work."""
    stem = name.rsplit("/", 1)[-1]
    return re.sub(r"[\s_\-.]+", " ", stem.lower()).strip()


def classify(filename: str, metadata: Optional[Mapping[str, Any]] = None) -> tuple[str, Optional[str]]:
    """Return (section key, doc_type) for a file name, metadata winning over rules."""
    meta = metadata or {}
    doc_type = meta.get("doc_type")
    section = meta.get("section")
    if section is not None:
        section = _section_key(section)
    if doc_type is None:
        norm = normalize_name(filename)
        for t, _s, _lbl, pat in DOC_TYPE_RULES:
            if re.search(pat, norm):
                doc_type = t
                break
    if section is None:
        section = DOC_TYPE_SECTION.get(doc_type or "", UNCLASSIFIED)
    return section, doc_type


def _section_key(value: Any) -> str:
    s = str(value).strip()
    if s.isdigit() and f"{int(s):02d}" in SECTIONS:
        return f"{int(s):02d}"
    for k, v in SECTIONS.items():
        if s.lower() in (v.lower(), SECTION_FOLDERS[k].lower()):
            return k
    raise InputError(f"Unknown data room section {value!r}; use 1-8 or one of: {', '.join(SECTIONS.values())}")


# ------------------------------------------------------------------ checklist

@dataclass
class ChecklistItem:
    item: str
    label: str
    section: str
    level: str  # required | recommended
    reason: str
    satisfied_by_types: tuple[str, ...]
    match_terms: tuple[str, ...] = ()
    status: str = "missing"
    satisfied_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"item": self.item, "label": self.label, "section": self.section,
                "section_name": SECTIONS.get(self.section, "Unclassified"), "level": self.level,
                "reason": self.reason, "status": self.status, "satisfied_by": list(self.satisfied_by)}


def required_items(lineage: Mapping[str, Any]) -> list[ChecklistItem]:
    """Stage-aware checklist for the asset, plus items implied by its lineage."""
    asset = lineage.get("asset") or {}
    stage = asset.get("stage") or "discovery"
    if stage not in _STAGE_INDEX:
        raise InputError(f"asset.stage {stage!r} is not a registry stage")
    idx = _STAGE_INDEX[stage]
    at = lambda s: idx >= _STAGE_INDEX[s]  # noqa: E731

    items: list[ChecklistItem] = []

    def add(t: str, level: str, reason: str, types: Iterable[str] = (), terms: Iterable[str] = (),
            item: Optional[str] = None, label: Optional[str] = None) -> None:
        items.append(ChecklistItem(item or t, label or DOC_TYPE_LABEL[t], DOC_TYPE_SECTION[t], level, reason,
                                   tuple(types) or (t,), tuple(terms)))

    add("target_validation", "required", "all stages")
    add("in_vitro_pharmacology", "required" if at("lead_optimization") else "recommended", "lead optimization onward")
    if at("candidate"):
        for t in ("tpp", "preliminary_tox", "cmc_feasibility"):
            add(t, "required", "candidate stage onward")
        add("provisional_patent", "required", "candidate stage onward", types=("provisional_patent", "patent"))
    if at("ind_enabling"):
        for t in ("glp_tox", "cmc_module3", "pre_ind_minutes"):
            add(t, "required", "IND-enabling stage onward")
    if at("ind_filed"):
        add("ind_application", "required", "IND filed onward")
    if at("phase1"):
        add("protocol", "required", "clinical stage")
        add("investigator_brochure", "required", "clinical stage")
        studies = lineage.get("studies") or []
        done = [s for s in studies if s.get("kind") == "clinical" and s.get("status") == "complete"]
        if at("phase2") or done:
            why = "a clinical study is complete" if done else "Phase 2 onward"
            add("csr", "required", why)
    if at("phase1") or (lineage.get("batches") or []):
        add("batch_record", "recommended", "clinical or GMP batches exist")

    for s in lineage.get("submissions") or []:
        if s.get("kind") == "designation" and s.get("status") in ("granted", "approved") and s.get("designation"):
            d = str(s["designation"])
            words = {"fast_track": "fast track", "priority_review": "priority review"}.get(d, d)
            add("designation_letter", "recommended", f"{s.get('agency', '')} {d} designation granted ({s.get('submission_id')})",
                terms=(words, "odd" if d == "orphan" else words), item=f"designation_letter:{d}",
                label=f"{words.upper() if len(words) <= 5 else words.title()} designation letter")
    partners = {p.get("partner_id"): p for p in lineage.get("partners") or []}
    for d in lineage.get("deals") or []:
        if d.get("status") == "signed":
            p = partners.get(d.get("partner_id")) or {}
            terms = [str(d.get("deal_id", "")).lower(), *str(d.get("deal_type", "")).lower().split("_")]
            if p.get("kind"):
                terms.append(str(p["kind"]).lower())
            add("contract", "recommended", f"signed {d.get('deal_type')} deal {d.get('deal_id')}",
                terms=tuple(t for t in terms if t), item=f"contract:{d.get('deal_id')}",
                label=f"Agreement for {d.get('deal_id')} ({p.get('name') or d.get('partner_id')})")
    return items


def evaluate_checklist(lineage: Mapping[str, Any], documents: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Mark each checklist item present/missing given documents with name and doc_type."""
    docs = list(documents)
    items = required_items(lineage)
    for it in items:
        for d in docs:
            if d.get("doc_type") not in it.satisfied_by_types:
                continue
            if it.match_terms:
                hay = normalize_name(str(d.get("name", ""))) + " " + normalize_name(json.dumps(d.get("meta") or {}))
                if not any(t and t in hay for t in it.match_terms):
                    continue
            it.satisfied_by.append(str(d.get("path") or d.get("name")))
        it.status = "present" if it.satisfied_by else "missing"
    return [it.to_dict() for it in items]


def gaps_for_names(lineage: Mapping[str, Any], names: Iterable[str]) -> dict[str, Any]:
    """Checklist for a list of file names (no file access), used by the API."""
    docs = []
    for n in names:
        sec, t = classify(str(n))
        docs.append({"name": str(n), "path": str(n), "doc_type": t, "section": sec})
    checklist = evaluate_checklist(lineage, docs)
    return {
        "asset_id": lineage.get("asset_id"),
        "stage": (lineage.get("asset") or {}).get("stage"),
        "checklist": checklist,
        "gaps": [c for c in checklist if c["status"] == "missing"],
        "unclassified": [d["name"] for d in docs if d["section"] == UNCLASSIFIED],
    }


# ------------------------------------------------------------- digital thread

@dataclass
class ThreadNode:
    id: str
    type: str = ""
    title: str = ""
    uri: str = ""


@dataclass
class Thread:
    nodes: list[ThreadNode] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _first(d: Mapping[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def load_thread(source: Union[str, Path, Mapping[str, Any], None]) -> Thread:
    """Tolerant DigitalThreadBuilder export importer.

    Accepts ``{"nodes": [...], "edges": [...]}`` (also ``links``/``relationships``,
    or a ``graph`` wrapper). Node keys tried: id/node_id/key, type/kind/node_type/label,
    title/name/label, uri/url/path/file/document. Edge keys: source/from/src and
    target/to/dst, type/relation/label. Malformed entries become warnings.
    """
    if source is None:
        return Thread()
    if isinstance(source, Mapping):
        data: Any = source
    elif not isinstance(source, (str, Path)):
        raise InputError("Digital thread export must be a file path or a JSON object")
    else:
        try:
            data = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise InputError(f"Cannot read digital thread export {source}: {exc}") from exc
    if isinstance(data, Mapping) and isinstance(data.get("graph"), Mapping):
        data = data["graph"]
    if not isinstance(data, Mapping):
        raise InputError("Digital thread export must be a JSON object with 'nodes'")
    t = Thread()
    nodes_raw = data.get("nodes") or []
    if isinstance(nodes_raw, Mapping):  # {"RD-001": {...}}
        nodes_raw = [dict(v, id=k) if isinstance(v, Mapping) else {"id": k} for k, v in nodes_raw.items()]
    for i, n in enumerate(nodes_raw):
        if not isinstance(n, Mapping):
            t.warnings.append(f"thread node #{i} is not an object; skipped")
            continue
        nid = _first(n, "id", "node_id", "key")
        if not nid:
            t.warnings.append(f"thread node #{i} has no id; skipped")
            continue
        props = n.get("properties") if isinstance(n.get("properties"), Mapping) else {}
        merged = {**props, **n}
        t.nodes.append(ThreadNode(nid, _first(merged, "type", "kind", "node_type", "label"),
                                  _first(merged, "title", "name", "label"),
                                  _first(merged, "uri", "url", "path", "file", "document")))
    for i, e in enumerate(data.get("edges") or data.get("links") or data.get("relationships") or []):
        if not isinstance(e, Mapping):
            t.warnings.append(f"thread edge #{i} is not an object; skipped")
            continue
        src, dst = _first(e, "source", "from", "src"), _first(e, "target", "to", "dst")
        if not src or not dst:
            t.warnings.append(f"thread edge #{i} lacks source/target; skipped")
            continue
        t.edges.append({"source": src, "target": dst, "type": _first(e, "type", "relation", "label")})
    return t


def thread_section(node: ThreadNode) -> Optional[str]:
    prefix = node.id.split("-", 1)[0].upper()
    if prefix in THREAD_PREFIX_SECTION:
        return THREAD_PREFIX_SECTION[prefix]
    low = f"{node.type} {node.title}".lower()
    for pat, sec in THREAD_TYPE_SECTION:
        if re.search(pat, low):
            return sec
    return None


# ------------------------------------------------------------------- safety

def _inside(root: Path, candidate: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(candidate)]) == str(root)
    except ValueError:
        return False


def safe_resolve(root: Path, rel: str) -> Optional[Path]:
    """Resolve ``rel`` under ``root``; None if it escapes root or passes through a symlink."""
    if not rel or "\x00" in rel:
        return None
    if rel.startswith("file://"):
        rel = rel[len("file://"):]
    p = Path(rel)
    candidate = Path(os.path.abspath(p if p.is_absolute() else root / p))  # collapses ".."
    if not _inside(root, candidate):
        return None
    cur = root
    for part in candidate.relative_to(root).parts:  # no symlink anywhere along the path
        cur = cur / part
        if cur.is_symlink():
            return None
    real = Path(os.path.realpath(candidate))
    return real if _inside(root, real) else None


def iter_documents(root: Path) -> tuple[list[Path], list[str]]:
    """Regular files under root, no symlinks, no sidecar .meta.json, no dotfiles."""
    files: list[Path] = []
    warnings: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        for dn in list(dirnames):
            if (d / dn).is_symlink():
                warnings.append(f"skipped symlinked folder {(d / dn).relative_to(root)}")
                dirnames.remove(dn)
            elif dn.startswith("."):
                dirnames.remove(dn)
        for fn in sorted(filenames):
            p = d / fn
            rel = p.relative_to(root)
            if fn.startswith(".") or fn.endswith(".meta.json"):
                continue
            if p.is_symlink():
                warnings.append(f"skipped symlink {rel} (symlinks are never followed)")
                continue
            real = Path(os.path.realpath(p))
            if not _inside(root, real) or not real.is_file():
                warnings.append(f"skipped {rel}: not a regular file inside the documents directory")
                continue
            files.append(p)
    return sorted(files), warnings


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")
    return cleaned or "document"


# ------------------------------------------------------------------- builder

@dataclass
class DataRoomResult:
    out_dir: Path
    zip_path: Optional[Path]
    index: dict[str, Any]


def _valuation_files(lineage: Mapping[str, Any], overrides: Optional[Mapping[str, Any]]) -> tuple[str, Optional[dict], Optional[str]]:
    """Markdown summary, JSON (snapshot + sensitivity + scenarios) or None, error or None."""
    from .engine import scenarios, sensitivity, value
    from .exports import valuation_snapshot

    asset_id = lineage.get("asset_id")
    try:
        v = value(lineage, overrides)
        sens = sensitivity(lineage, overrides)
        scen = scenarios(lineage, overrides)
    except InputError as exc:
        md = (f"# Valuation summary: {asset_id}\n\nNot computed: {exc}\n\n"
              "Fill the missing registry field or pass an override, then rebuild the room.\n")
        return md, None, str(exc)
    inp = v.inputs
    lines = [
        f"# Valuation summary: {inp.asset_name} ({inp.asset_id})", "",
        f"As of {v.as_of}. USD millions. Method: rNPV ({v.engine_source}).", "",
        "| Measure | Value |", "|---|---|",
        f"| rNPV | {v.rnpv:,.1f} |", f"| NPV unrisked | {v.npv_unrisked:,.1f} |",
        f"| Cumulative PoS | {v.cumulative_pos:.3f} |", f"| Peak sales | {inp.peak_sales_millions:,.0f} |",
        f"| Discount rate | {inp.discount_rate:.1%} |", f"| Years to launch | {inp.years_to_launch} |",
        f"| Patent life (years) | {inp.patent_life_years} |", f"| COGS % of sales | {inp.cogs_pct:.1%} |",
        f"| Bear / base / bull rNPV | {scen['bear']['rnpv']:,.1f} / {scen['base']['rnpv']:,.1f} / {scen['bull']['rnpv']:,.1f} |",
        "", "## Sensitivity (largest swing first)", "", "| Driver | Low case | High case |", "|---|---|---|",
    ]
    for r in sens["rows"]:
        lines.append(f"| {r['label']} | {r['low']['label']}: {r['low']['rnpv']:,.1f} | "
                     f"{r['high']['label']}: {r['high']['rnpv']:,.1f} |")
    lines += ["", "## Input sources", "", "| Field | Source system | Reference |", "|---|---|---|"]
    lines += [f"| {s['field']} | {s['source_system']} | {s['ref']} |" for s in v.sources]
    if v.warnings:
        lines += ["", "## Flags", ""] + [f"- {w}" for w in v.warnings]
    lines += ["", "Illustrative defaults are planning assumptions, not facts. Replace them before relying on the number.", ""]
    payload = {"snapshot": valuation_snapshot(v), "sensitivity": sens, "scenarios": scen}
    return "\n".join(lines), payload, None


def build_dataroom(lineage: Mapping[str, Any], docs_dir: Union[str, Path], out_dir: Union[str, Path], *,
                   thread: Union[str, Path, Mapping[str, Any], None] = None,
                   overrides: Optional[Mapping[str, Any]] = None, make_zip: bool = False,
                   force: bool = False) -> DataRoomResult:
    """Assemble the data room folder (and zip) for one asset."""
    asset_id = str(lineage.get("asset_id") or "")
    if not re.match(r"^AST-\d{6}$", asset_id):
        raise InputError(f"lineage.asset_id must match AST-NNNNNN, got {asset_id!r}")
    root = Path(os.path.realpath(docs_dir))
    if not root.is_dir():
        raise InputError(f"Documents directory not found: {docs_dir}")
    out = Path(os.path.abspath(out_dir))
    if _inside(root, Path(os.path.realpath(out))):
        raise InputError("Output directory must not be inside the documents directory")
    if out.exists() and any(out.iterdir()):
        if not force:
            raise InputError(f"Output directory {out} is not empty; pass force=True (--force) to replace it")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    th = load_thread(thread)
    warnings.extend(th.warnings)

    files, walk_warn = iter_documents(root)
    warnings.extend(walk_warn)

    # thread nodes -> documents inside root
    node_by_file: dict[Path, list[ThreadNode]] = {}
    external_refs: list[dict[str, str]] = []
    missing_thread: list[dict[str, str]] = []
    for n in th.nodes:
        if not n.uri:
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*://", n.uri, re.I) and not n.uri.lower().startswith("file://"):
            external_refs.append({"node_id": n.id, "type": n.type, "title": n.title, "uri": n.uri})
            continue
        resolved = safe_resolve(root, n.uri)
        if resolved is None:
            warnings.append(f"thread node {n.id}: uri rejected (outside the documents directory or via a symlink)")
            continue
        if not resolved.is_file():
            missing_thread.append({"node_id": n.id, "title": n.title, "uri": n.uri})
            continue
        node_by_file.setdefault(resolved, []).append(n)

    used: set[str] = set()
    documents: list[dict[str, Any]] = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        meta: dict[str, Any] = {}
        side = p.with_name(p.name + ".meta.json")
        if side.is_file() and not side.is_symlink():
            try:
                meta = json.loads(side.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                warnings.append(f"{rel}.meta.json is not valid JSON; ignored")
        if meta.get("asset_id") and meta["asset_id"] != asset_id:
            warnings.append(f"skipped {rel}: metadata says it belongs to {meta['asset_id']}")
            continue
        section, doc_type = classify(rel, meta)
        nodes = node_by_file.get(Path(os.path.realpath(p)), [])
        if section == UNCLASSIFIED:
            for n in nodes:
                s = thread_section(n)
                if s:
                    section = s
                    break
        folder = SECTION_FOLDERS[section]
        name = _safe_name(p.name)
        dest_rel = f"{folder}/{name}"
        k = 2
        while dest_rel in used:
            stem, dot, ext = name.rpartition(".")
            dest_rel = f"{folder}/{stem}-{k}.{ext}" if dot else f"{folder}/{name}-{k}"
            k += 1
        used.add(dest_rel)
        dest = out / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
        documents.append({
            "path": dest_rel, "name": p.name, "original_path": rel, "section": section,
            "section_name": SECTIONS.get(section, "Unclassified"), "doc_type": doc_type,
            "sha256": sha256_file(dest), "size": dest.stat().st_size,
            "source": "digital_thread+documents_dir" if nodes else "documents_dir",
            "asset_id": asset_id, "thread_nodes": [n.id for n in nodes], "meta": meta,
        })

    # generated valuation summary
    md, val_payload, val_error = _valuation_files(lineage, overrides)
    vfolder = out / SECTION_FOLDERS["07"]
    vfolder.mkdir(parents=True, exist_ok=True)
    gen: list[tuple[str, str]] = [(f"{SECTION_FOLDERS['07']}/valuation_summary.md", md)]
    if val_payload is not None:
        gen.append((f"{SECTION_FOLDERS['07']}/valuation.json", json.dumps(val_payload, indent=2)))
    for rel_path, text in gen:
        (out / rel_path).write_text(text, encoding="utf-8")
        documents.append({
            "path": rel_path, "name": Path(rel_path).name, "original_path": None, "section": "07",
            "section_name": SECTIONS["07"], "doc_type": "valuation_summary",
            "sha256": sha256_file(out / rel_path), "size": (out / rel_path).stat().st_size,
            "source": "generated:valuation_room", "asset_id": asset_id, "thread_nodes": [], "meta": {},
        })

    checklist = evaluate_checklist(lineage, documents)
    gaps = [c for c in checklist if c["status"] == "missing"]
    unclassified = [d["original_path"] for d in documents if d["section"] == UNCLASSIFIED]

    asset = lineage.get("asset") or {}
    gaps_md = _gaps_markdown(asset_id, asset, gaps, unclassified, missing_thread, val_error, warnings)
    (out / "gaps.md").write_text(gaps_md, encoding="utf-8")

    index: dict[str, Any] = {
        "asset_id": asset_id, "asset_name": asset.get("name"), "stage": asset.get("stage"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": "AssetValuationDataRoom v0.1",
        "sections": {k: SECTION_FOLDERS[k] for k in SECTIONS},
        "documents": documents,
        "gaps_file": {"path": "gaps.md", "sha256": sha256_file(out / "gaps.md"), "size": (out / "gaps.md").stat().st_size},
        "checklist": checklist, "gaps": gaps, "unclassified": unclassified,
        "thread": {"nodes": len(th.nodes), "edges": th.edges, "external_references": external_refs,
                   "missing_files": missing_thread},
        "valuation_error": val_error, "warnings": warnings,
    }
    (out / "index.html").write_text(_index_html(index), encoding="utf-8")
    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    zip_path = None
    if make_zip:
        zip_path = out.parent / f"{out.name}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    zf.write(f, f.relative_to(out).as_posix())
    return DataRoomResult(out, zip_path, index)


def _gaps_markdown(asset_id: str, asset: Mapping[str, Any], gaps: list[dict], unclassified: list[str],
                   missing_thread: list[dict], val_error: Optional[str], warnings: list[str]) -> str:
    lines = [f"# Data room gaps: {asset.get('name') or asset_id} ({asset_id})", "",
             f"Stage: {asset.get('stage')}. Checklist is stage-aware; recommended items come from the lineage record.", ""]
    req = [g for g in gaps if g["level"] == "required"]
    rec = [g for g in gaps if g["level"] == "recommended"]
    lines += ["## Missing required documents", ""]
    lines += [f"- [{g['section']} {g['section_name']}] {g['label']} ({g['reason']})" for g in req] or ["- None"]
    lines += ["", "## Missing recommended documents", ""]
    lines += [f"- [{g['section']} {g['section_name']}] {g['label']} ({g['reason']})" for g in rec] or ["- None"]
    lines += ["", "## Documents that need manual filing (unclassified)", ""]
    lines += [f"- {u}" for u in unclassified] or ["- None"]
    lines += ["", "## Digital thread references with no file in the documents directory", ""]
    lines += [f"- {m['node_id']}: {m['title'] or ''} ({m['uri']})" for m in missing_thread] or ["- None"]
    if val_error:
        lines += ["", "## Valuation", "", f"- Valuation not computed: {val_error}"]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    return "\n".join(lines) + "\n"


def _index_html(index: Mapping[str, Any]) -> str:
    e = html.escape
    rows = []
    for key, folder in [(k, SECTION_FOLDERS[k]) for k in [*SECTIONS, UNCLASSIFIED]]:
        docs = [d for d in index["documents"] if d["section"] == key]
        if not docs:
            continue
        rows.append(f"<tr class='sec'><th colspan='4'>{e(key)} {e(SECTIONS.get(key, 'Unclassified'))}</th></tr>")
        for d in docs:
            rows.append(
                f"<tr><td><a href='{e(d['path'], quote=True)}'>{e(d['name'])}</a></td>"
                f"<td>{e(DOC_TYPE_LABEL.get(d['doc_type'] or '', d['doc_type'] or '-'))}</td>"
                f"<td>{d['size']:,}</td><td><code>{e(d['sha256'][:16])}</code></td></tr>")
    gaps = "".join(f"<li>{e(g['label'])} <small>({e(g['level'])}; {e(g['reason'])})</small></li>"
                   for g in index["gaps"]) or "<li>None</li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data room {e(index['asset_id'])}</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:60rem;color:#1d1d1f;background:#fff}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left}}
tr.sec th{{background:#f2f4f7}}code{{font-size:.85em}}</style></head><body>
<h1>Diligence data room: {e(str(index.get('asset_name') or ''))} ({e(index['asset_id'])})</h1>
<p>Stage: {e(str(index.get('stage')))}. Generated {e(index['generated_at'])}. Full manifest with sha256: <a href="index.json">index.json</a>. Gaps: <a href="gaps.md">gaps.md</a>.</p>
<table><thead><tr><th>Document</th><th>Type</th><th>Bytes</th><th>sha256 (prefix)</th></tr></thead><tbody>
{''.join(rows)}
</tbody></table>
<h2>Gaps</h2><ul>{gaps}</ul>
</body></html>
"""
