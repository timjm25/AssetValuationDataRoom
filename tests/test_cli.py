"""Console script smoke tests."""
import csv
import io
import json

from valuation_room.cli import main
from valuation_room.exports import SOTP_COLUMNS

from conftest import AS_OF, EXAMPLES

LINEAGE = str(EXAMPLES / "lineage_AST-000002.json")


def test_value_text_and_json(capsys):
    assert main(["value", LINEAGE, "--as-of", AS_OF]) == 0
    out = capsys.readouterr().out
    assert "rNPV" in out and "195.0" in out and "PTSPredictor" in out
    assert main(["value", LINEAGE, "--as-of", AS_OF, "--json", "--override", "cogs_per_dose=95000",
                 "--override", "price_per_dose=475000"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["inputs"]["cogs_pct"] == 0.2


def test_sensitivity_and_exports(capsys):
    assert main(["sensitivity", LINEAGE, "--as-of", AS_OF]) == 0
    assert "Peak sales" in capsys.readouterr().out
    assert main(["export-sotp", LINEAGE, "--as-of", AS_OF]) == 0
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0] == SOTP_COLUMNS and len(rows) == 2
    assert main(["export-dealsim", LINEAGE, "--as-of", AS_OF, "--units", "millions"]) == 0
    assert json.loads(capsys.readouterr().out)["create_deal"]["deal_type"] == "license_out"
    assert main(["snapshot", LINEAGE, "--as-of", AS_OF]) == 0
    assert json.loads(capsys.readouterr().out)["correlation_id"] == "AST-000002"


def test_dataroom_build_and_errors(capsys, tmp_path):
    out = tmp_path / "room"
    assert main(["dataroom", "build", LINEAGE, "--docs", str(EXAMPLES / "docs"), "--out", str(out),
                 "--thread", str(EXAMPLES / "thread_AST-000002.json"), "--zip", "--as-of", AS_OF]) == 0
    assert "2 required gap(s)" in capsys.readouterr().out
    assert (tmp_path / "room.zip").exists()
    assert main(["value", str(tmp_path / "missing.json")]) == 2
    assert "error:" in capsys.readouterr().err
