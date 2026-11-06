"""Data room: sections, stage-aware gaps, manifest, path traversal guard, zip."""
import hashlib
import json
import os
import zipfile

import pytest

from valuation_room.dataroom import (build_dataroom, classify, gaps_for_names, load_thread,
                                     safe_resolve)
from valuation_room.inputs import InputError


@pytest.mark.parametrize("name,section,doc_type", [
    ("target_product_profile_VR-201.md", "01", "tpp"),
    ("US_provisional_patent.txt", "02", "provisional_patent"),
    ("GLP_tox_report.pdf", "03", "glp_tox"),
    ("dose_range_finding_tox.md", "03", "preliminary_tox"),
    ("cmc_module3_draft.docx", "04", "cmc_module3"),
    ("CMC_feasibility_assessment.pdf", "04", "cmc_feasibility"),
    ("Clinical_Study_Report_101.pdf", "05", "csr"),
    ("pre-IND_meeting_minutes.pdf", "06", "pre_ind_minutes"),
    ("market_forecast.xlsx", "07", "commercial"),
    ("CDMO_master_services_agreement.pdf", "08", "contract"),
    ("random_notes.txt", "99", None),
])
def test_classify_by_filename(name, section, doc_type):
    assert classify(name) == (section, doc_type)


def test_classify_metadata_wins():
    assert classify("random.txt", {"section": "IP", "doc_type": "patent"}) == ("02", "patent")
    assert classify("random.txt", {"section": 5}) == ("05", None)


def test_gaps_by_stage(cart):
    names = ["target_validation.md", "in_vitro_pharmacology.md"]
    cart["asset"]["stage"] = "candidate"
    cand = {g["item"] for g in gaps_for_names(cart, names)["gaps"] if g["level"] == "required"}
    assert cand == {"tpp", "preliminary_tox", "provisional_patent", "cmc_feasibility"}
    cart["asset"]["stage"] = "ind_filed"
    ind = {g["item"] for g in gaps_for_names(cart, names)["gaps"] if g["level"] == "required"}
    assert {"glp_tox", "cmc_module3", "ind_application", "pre_ind_minutes"} <= ind
    assert "protocol" not in ind
    cart["asset"]["stage"] = "phase2"
    clin = {g["item"] for g in gaps_for_names(cart, names)["gaps"] if g["level"] == "required"}
    assert {"csr", "investigator_brochure", "protocol"} <= clin


def test_csr_required_in_phase1_only_when_a_clinical_study_completed(cart):
    req = lambda: {g["item"] for g in gaps_for_names(cart, [])["gaps"]}  # noqa: E731
    assert "csr" not in req()
    cart["studies"][-1]["status"] = "complete"
    assert "csr" in req()


def test_build_dataroom_example(cart, ov, docs_dir, thread_file, tmp_path):
    out = tmp_path / "room"
    res = build_dataroom(cart, docs_dir, out, thread=thread_file, overrides=ov, make_zip=True)
    idx = json.loads((out / "index.json").read_text())
    assert idx["asset_id"] == "AST-000002"
    by_name = {d["name"]: d for d in idx["documents"]}
    assert by_name["GLP_tox_biodistribution_report_EXAMPLE.md"]["path"].startswith("03_Discovery_and_Preclinical/")
    assert by_name["cmc_module3_draft_EXAMPLE.md"]["section"] == "04"
    assert by_name["RMAT_designation_letter_EXAMPLE.txt"]["section"] == "06"
    # thread node classifies an otherwise unclassified file
    notes = by_name["lab_notebook_scan_notes_EXAMPLE.txt"]
    assert notes["section"] == "03" and notes["thread_nodes"] == ["RD-003"]
    assert by_name["batch_record_ENG-001_EXAMPLE.md"]["thread_nodes"] == ["MFG-002"]
    # manifest integrity
    for d in idx["documents"]:
        data = (out / d["path"]).read_bytes()
        assert d["sha256"] == hashlib.sha256(data).hexdigest() and d["size"] == len(data)
        assert d["asset_id"] == "AST-000002" and d["source"]
    # gaps for a phase 1 CAR-T with this fixture folder
    required = {g["item"] for g in idx["gaps"] if g["level"] == "required"}
    assert required == {"preliminary_tox", "cmc_feasibility"}
    assert {g["item"] for g in idx["gaps"] if g["level"] == "recommended"} == {"designation_letter:orphan"}
    assert idx["thread"]["missing_files"][0]["node_id"] == "RD-002"
    assert idx["thread"]["external_references"][0]["node_id"] == "MFG-003"
    gaps_md = (out / "gaps.md").read_text()
    assert "Preliminary (non-GLP) toxicology" in gaps_md and "RD-002" in gaps_md
    assert "AST-000002" in (out / "index.html").read_text()
    summary = (out / "07_Commercial_and_Valuation" / "valuation_summary.md").read_text()
    assert "195.0" in summary and "PTSPredictor" in summary
    # zip mirrors the folder
    with zipfile.ZipFile(res.zip_path) as zf:
        names = set(zf.namelist())
    assert {"index.json", "index.html", "gaps.md", "07_Commercial_and_Valuation/valuation_summary.md"} <= names
    assert {d["path"] for d in idx["documents"]} <= names


def test_path_traversal_and_symlinks_are_never_copied(cart, ov, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "target_validation.md").write_text("EXAMPLE")
    secret = tmp_path / "secret_agreement.txt"
    secret.write_text("TOP SECRET")
    os.symlink(secret, docs / "linked_agreement.txt")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "patent.txt").write_text("outside")
    os.symlink(outside_dir, docs / "linked_dir")
    thread = {"nodes": [
        {"id": "RD-009", "type": "Study", "uri": "../secret_agreement.txt"},
        {"id": "RD-010", "type": "Study", "uri": str(secret)},
        {"id": "RD-011", "type": "Study", "uri": "linked_dir/patent.txt"},
    ]}
    res = build_dataroom(cart, docs, tmp_path / "room", thread=thread, overrides=ov)
    copied = [p for p in (tmp_path / "room").rglob("*") if p.is_file()]
    assert all("TOP SECRET" not in p.read_text(errors="ignore") for p in copied)
    assert all(p.read_text(errors="ignore") != "outside" for p in copied)
    names = {d["name"] for d in res.index["documents"]}
    assert "linked_agreement.txt" not in names and "patent.txt" not in names
    rejected = [w for w in res.index["warnings"] if "uri rejected" in w]
    assert len(rejected) == 3
    assert any("symlink" in w for w in res.index["warnings"])


def test_safe_resolve(tmp_path):
    root = tmp_path.resolve()
    (root / "a.txt").write_text("x")
    assert safe_resolve(root, "a.txt") == root / "a.txt"
    assert safe_resolve(root, "sub/../a.txt") == root / "a.txt"
    assert safe_resolve(root, "../a.txt") is None
    assert safe_resolve(root, "/etc/passwd") is None
    assert safe_resolve(root, "") is None


def test_output_guards(cart, ov, docs_dir, tmp_path):
    with pytest.raises(InputError, match="inside the documents directory"):
        build_dataroom(cart, docs_dir, docs_dir / "room", overrides=ov)
    out = tmp_path / "room"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(InputError, match="not empty"):
        build_dataroom(cart, docs_dir, out, overrides=ov)
    build_dataroom(cart, docs_dir, out, overrides=ov, force=True)
    assert not (out / "keep.txt").exists()


def test_valuation_failure_becomes_a_gap(cart, ov, docs_dir, tmp_path):
    cart["indications"][0]["peak_sales_millions"] = None
    res = build_dataroom(cart, docs_dir, tmp_path / "room", overrides=ov)
    assert "peak_sales_millions" in res.index["valuation_error"]
    assert "Valuation not computed" in (tmp_path / "room" / "gaps.md").read_text()


def test_tolerant_thread_importer():
    t = load_thread({"graph": {"nodes": [{"node_id": "MFG-002", "kind": "Batch", "name": "ENG-001", "path": "x.md"},
                                         {"title": "no id"}, "junk"],
                               "links": [{"from": "MFG-002", "to": "RD-001", "relation": "uses"}, {"from": "x"}]}})
    assert [(n.id, n.type, n.title, n.uri) for n in t.nodes] == [("MFG-002", "Batch", "ENG-001", "x.md")]
    assert t.edges == [{"source": "MFG-002", "target": "RD-001", "type": "uses"}]
    assert len(t.warnings) == 3
    keyed = load_thread({"nodes": {"RD-001": {"type": "Study"}}})
    assert keyed.nodes[0].id == "RD-001"
    with pytest.raises(InputError):
        load_thread([])  # type: ignore[arg-type]
