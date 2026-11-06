"""Shared fixtures: EXAMPLE lineage records and a fixed valuation date."""
import copy
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
AS_OF = "2026-11-06"


def _load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture
def cart():
    """EXAMPLE autologous CAR-T at phase 1 (AST-000002)."""
    return copy.deepcopy(_load("lineage_AST-000002.json"))


@pytest.fixture
def small_molecule():
    """EXAMPLE small molecule at phase 2 (AST-000001)."""
    return copy.deepcopy(_load("lineage_AST-000001.json"))


@pytest.fixture
def ov():
    return {"as_of": AS_OF}


@pytest.fixture
def docs_dir():
    return EXAMPLES / "docs"


@pytest.fixture
def thread_file():
    return EXAMPLES / "thread_AST-000002.json"
