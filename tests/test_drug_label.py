"""Tests for get_drug_label.

Fast tests (default):   uv run pytest
Live API tests:         uv run pytest -m live
"""
import json

import pytest
from fastmcp import Client

import main
from label_format import MAX_TOTAL_CHARS, summarize_label
from openfda_client import choose_term, is_repackager, pick_label, search_drug_label

# Claude's MCP clients cut tool output off at 25,000 tokens.
# JSON runs roughly 4 characters per token, so stay well under 100,000 chars.
CLIENT_LIMIT_CHARS = 100_000


@pytest.fixture
def anyio_backend():
    return "asyncio"


def fake_label(**overrides) -> dict:
    """A label shaped like OpenFDA's, with deliberately huge sections."""
    big = "Lactic acidosis risk. " * 2000  # ~44,000 characters
    label = {
        "set_id": "abc-123",
        "effective_time": "20250101",
        "openfda": {
            "brand_name": ["Metformin Hydrochloride"],
            "generic_name": ["METFORMIN HYDROCHLORIDE"],
            "manufacturer_name": ["Example Pharma"],
            "route": ["ORAL"],
        },
        "boxed_warning": [big],
        "indications_and_usage": ["Adjunct to diet and exercise in type 2 diabetes."],
        "dosage_and_administration": [big],
        "contraindications": ["Severe renal impairment (eGFR below 30)."],
        "warnings_and_cautions": [big],
        "adverse_reactions": [big],
        "adverse_reactions_table": ["<table>" + "<tr><td>x</td></tr>" * 5000 + "</table>"],
        "clinical_pharmacology_table": ["<table>" + "<tr><td>y</td></tr>" * 5000 + "</table>"],
    }
    label.update(overrides)
    return label


# --- Which product? ---------------------------------------------------------

METFORMIN_GENERICS = [
    {"term": "METFORMIN HYDROCHLORIDE", "count": 261},
    {"term": "SITAGLIPTIN AND METFORMIN HYDROCHLORIDE", "count": 12},
    {"term": "GLIPIZIDE AND METFORMIN HYDROCHLORIDE", "count": 9},
]


def test_plain_generic_picks_single_ingredient_not_combination():
    field, term, _ = choose_term("metformin", [], METFORMIN_GENERICS)
    assert field == "openfda.generic_name.exact"
    assert term == "METFORMIN HYDROCHLORIDE"


def test_generic_beats_a_brand_that_is_just_the_generic_name():
    # Found by the live test: one label files "Metformin" as its brand name
    # with the generic "METFORMIN ER 500 MG". It must not beat plain metformin.
    brands = [{"term": "METFORMIN", "count": 1}]
    generics = METFORMIN_GENERICS + [{"term": "METFORMIN ER 500 MG", "count": 1}]
    _, term, _ = choose_term("metformin", brands, generics)
    assert term == "METFORMIN HYDROCHLORIDE"


def test_stray_generic_equal_to_query_loses_to_standard_name():
    # Found by the live test: a few labels list the generic as just
    # "METFORMIN". The standard name on hundreds of labels must win.
    generics = METFORMIN_GENERICS + [{"term": "METFORMIN", "count": 3}]
    _, term, _ = choose_term("metformin", [], generics)
    assert term == "METFORMIN HYDROCHLORIDE"


def test_brand_name_wins_when_typed_exactly():
    brands = [{"term": "GLUCOPHAGE", "count": 2}]
    field, term, _ = choose_term("Glucophage", brands, [])
    assert (field, term) == ("openfda.brand_name.exact", "GLUCOPHAGE")


def test_typed_combination_returns_the_combination():
    _, term, _ = choose_term(
        "sitagliptin and metformin hydrochloride", [], METFORMIN_GENERICS
    )
    assert term == "SITAGLIPTIN AND METFORMIN HYDROCHLORIDE"


def test_no_clean_match_returns_none():
    assert choose_term("zzz", [], METFORMIN_GENERICS) is None


# --- Whose label? ------------------------------------------------------------

def _by(maker: str, date: str) -> dict:
    return {"effective_time": date, "openfda": {"manufacturer_name": [maker]}}


def test_repackagers_are_recognised():
    assert is_repackager(_by("REMEDYREPACK INC.", "1"))
    assert is_repackager(_by("Bryant Ranch Prepack", "1"))
    assert not is_repackager(_by("Granules Pharmaceuticals Inc", "1"))


def test_original_manufacturer_beats_newer_repackager():
    labels = [
        _by("REMEDYREPACK INC.", "20260821"),
        _by("Granules Pharmaceuticals Inc", "20250110"),
        _by("Zydus Pharmaceuticals USA Inc.", "20251201"),
    ]
    assert pick_label(labels)["openfda"]["manufacturer_name"] == [
        "Zydus Pharmaceuticals USA Inc."
    ]


def test_repackager_used_when_nothing_else_exists():
    labels = [_by("REMEDYREPACK INC.", "20260821"), _by("NuCare Pharmaceuticals", "20240101")]
    assert pick_label(labels)["openfda"]["manufacturer_name"] == ["REMEDYREPACK INC."]


# --- How big is the answer? --------------------------------------------------

def test_summary_drops_tables_and_stays_small():
    out = summarize_label(fake_label())
    text = json.dumps(out)
    assert "<table>" not in text
    assert len(text) < MAX_TOTAL_CHARS + 2_000  # a little room for field names
    assert "boxed_warning" in out["truncated_sections"]


def test_summary_reports_sections_the_label_lacks():
    out = summarize_label(fake_label(), sections=["pregnancy"])
    assert out["missing_sections"] == ["pregnancy"]


def test_summary_ignores_unknown_section_names():
    out = summarize_label(fake_label(), sections=["contraindications", "bogus"])
    assert out["ignored_sections"] == ["bogus"]
    assert "contraindications" in out["sections"]


@pytest.mark.anyio
async def test_tool_output_fits_client_limit(monkeypatch):
    """Call the tool through a real MCP client, as Claude would."""
    async def fake_search(name):
        return {"label": fake_label(), "match": "test", "matched_name": name,
                "label_source": "manufacturer", "other_names": []}

    monkeypatch.setattr(main, "search_drug_label", fake_search)
    async with Client(main.mcp) as client:
        result = await client.call_tool("get_drug_label", {"drug_name": "metformin"})
    text = result.content[0].text
    assert len(text) < CLIENT_LIMIT_CHARS
    assert "_table" not in text


# --- Against the real OpenFDA API ------------------------------------------

@pytest.mark.live
@pytest.mark.anyio
async def test_live_metformin_is_plain_metformin():
    result = await search_drug_label("metformin")
    generics = result["label"]["openfda"]["generic_name"]
    assert generics == ["METFORMIN HYDROCHLORIDE"], (
        f"got {generics}; matched {result['matched_name']!r} by "
        f"{result['match']}; other names: {result['other_names']}"
    )


@pytest.mark.live
@pytest.mark.anyio
async def test_live_metformin_label_is_from_original_manufacturer():
    result = await search_drug_label("metformin")
    maker = result["label"]["openfda"].get("manufacturer_name")
    assert result["label_source"] == "manufacturer", f"got repackager {maker}"


@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("drug", [
    "metformin", "lisinopril", "atorvastatin", "warfarin", "Eliquis",
    "amoxicillin", "hydrochlorothiazide", "levothyroxine",
])
async def test_live_common_drugs_fit_and_are_single_ingredient(drug):
    async with Client(main.mcp) as client:
        result = await client.call_tool("get_drug_label", {"drug_name": drug})
    text = result.content[0].text
    data = json.loads(text)
    assert "error" not in data, data
    assert len(text) < CLIENT_LIMIT_CHARS
    for g in data["generic_name"]:
        assert " AND " not in g, f"{drug} matched a combination: {g}"
