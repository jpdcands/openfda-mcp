"""Tests for get_drug_label.

Fast tests (default):   uv run pytest
Live API tests:         uv run pytest -m live
"""
import json

import pytest
from fastmcp import Client

import main
from label_format import MAX_TOTAL_CHARS, summarize_label
from openfda_client import (
    choose_manufacturers, choose_term, is_modified_release, is_repackager,
    original_ndc_products, panel_labeler_codes, pick_label,
    pick_untagged_original, search_drug_label, split_release_request,
)

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


def test_manufacturer_list_drops_repackagers():
    makers = [
        {"term": "Cardinal Health 107, LLC", "count": 9},
        {"term": "E.R. Squibb & Sons, L.L.C.", "count": 2},
        {"term": "REMEDYREPACK INC.", "count": 5},
    ]
    assert choose_manufacturers(makers) == ["E.R. Squibb & Sons, L.L.C."]


def test_manufacturer_list_empty_when_all_repackagers():
    assert choose_manufacturers([{"term": "NuCare Pharmaceuticals", "count": 3}]) == []


# --- Untagged labels, identified by the NDC on the carton ------------------------
# Modelled on what OpenFDA really returned for ELIQUIS (Sep 2026).

ELIQUIS_NDC_DIRECTORY = [
    {"product_ndc": "70518-4462", "labeler_name": "REMEDYREPACK INC."},
    {"product_ndc": "82804-085", "labeler_name": "Proficient Rx LP"},
    # Listed first on purpose: the real directory returned the starter pack
    # before the tablets, and the label ended up described as the starter pack.
    {"product_ndc": "0003-3765", "labeler_name": "E.R. Squibb & Sons, L.L.C.",
     "brand_name": "ELIQUIS 30-Day Starter Pack", "generic_name": "apixaban"},
    {"product_ndc": "0003-0893", "labeler_name": "E.R. Squibb & Sons, L.L.C.",
     "brand_name": "Eliquis", "generic_name": "apixaban", "route": ["ORAL"]},
    {"product_ndc": "55154-0612", "labeler_name": "Cardinal Health 107, LLC"},
]


def _untagged(product: str, panel: str, date: str) -> dict:
    return {"effective_time": date, "spl_product_data_elements": [product],
            "package_label_principal_display_panel": [panel],
            "indications_and_usage": ["ELIQUIS is a factor Xa inhibitor indicated"]}


BMS_TABLETS = _untagged("ELIQUIS apixaban ANHYDROUS LACTOSE",
                        "NDC 0003-0893-21 ELIQUIS 5 mg 60 tablets", "20260130")
BMS_SPRINKLE = _untagged("ELIQUIS SPRINKLE apixaban",
                         "NDC 0003-3764-11 ELIQUIS SPRINKLE 0.15 mg", "20260401")
# A repackager that copied "Marketed by: Bristol-Myers Squibb" but has its own NDC.
COPYCAT = _untagged("ELIQUIS APIXABAN ANHYDROUS LACTOSE",
                    "ELIQUIS 5 mg Representative Packaging NDC 82982-054-30", "20260601")


def test_carton_ndc_labeler_codes_are_read():
    assert panel_labeler_codes(BMS_TABLETS) == {"0003"}
    assert panel_labeler_codes(COPYCAT) == {"82982"}


def test_ndc_directory_keeps_only_original_companies():
    assert list(original_ndc_products(ELIQUIS_NDC_DIRECTORY)) == ["0003"]


def test_untagged_original_chosen_over_copycat_and_sprinkle():
    originals = original_ndc_products(ELIQUIS_NDC_DIRECTORY)
    label = pick_untagged_original([COPYCAT, BMS_SPRINKLE, BMS_TABLETS], originals)
    assert label["package_label_principal_display_panel"] == BMS_TABLETS[
        "package_label_principal_display_panel"]
    assert label["openfda"]["manufacturer_name"] == ["E.R. Squibb & Sons, L.L.C."]
    assert label["openfda"]["generic_name"] == ["APIXABAN"]


def test_untagged_label_described_by_the_plain_product_entry():
    originals = original_ndc_products(ELIQUIS_NDC_DIRECTORY, "ELIQUIS")
    assert originals["0003"]["brand_name"] == "Eliquis"
    label = pick_untagged_original([BMS_TABLETS], originals)
    assert label["openfda"]["brand_name"] == ["Eliquis"]
    assert label["openfda"]["route"] == ["ORAL"]


def test_no_untagged_original_when_no_carton_matches():
    originals = original_ndc_products(ELIQUIS_NDC_DIRECTORY)
    assert pick_untagged_original([COPYCAT], originals) is None


# --- Immediate or extended release? --------------------------------------------

def _label(maker: str, date: str, indication: str) -> dict:
    return {"effective_time": date,
            "openfda": {"manufacturer_name": [maker]},
            "indications_and_usage": [indication]}


IR = "Metformin hydrochloride tablets are indicated as an adjunct to diet and exercise."
ER = "Metformin hydrochloride extended-release tablets is indicated as an adjunct."


@pytest.mark.parametrize("typed, expected", [
    ("metformin", ("metformin", False)),
    ("metformin ER", ("metformin", True)),
    ("Metformin XR", ("Metformin", True)),
    ("nifedipine extended-release", ("nifedipine", True)),
    ("sitagliptin and metformin", ("sitagliptin and metformin", False)),
])
def test_release_words_are_split_off(typed, expected):
    assert split_release_request(typed) == expected


def test_modified_release_is_recognised():
    assert is_modified_release(_label("Laurus", "1", ER))
    assert not is_modified_release(_label("Zydus", "1", IR))


def test_immediate_release_preferred_even_if_older():
    labels = [_label("Laurus Labs Limited", "20260519", ER),
              _label("Zydus Pharmaceuticals", "20250101", IR)]
    assert pick_label(labels)["indications_and_usage"] == [IR]


def test_extended_release_when_asked_for():
    labels = [_label("Laurus Labs Limited", "20260519", ER),
              _label("Zydus Pharmaceuticals", "20250101", IR)]
    assert pick_label(labels, wants_mr=True)["indications_and_usage"] == [ER]


def test_manufacturer_still_beats_release_type():
    labels = [_label("REMEDYREPACK INC.", "20260821", IR),
              _label("Laurus Labs Limited", "20260519", ER)]
    assert pick_label(labels)["openfda"]["manufacturer_name"] == ["Laurus Labs Limited"]


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
                "label_source": "manufacturer", "release": "immediate",
                "other_names": []}

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
async def test_live_eliquis_comes_from_bristol_myers_squibb():
    # Every label OpenFDA tags as ELIQUIS is a repackager's; Bristol-Myers
    # Squibb's own label is untagged and must be found by its carton NDC (0003).
    result = await search_drug_label("Eliquis")
    label = result["label"]
    assert result["label_source"].startswith("manufacturer"), result["label_source"]
    assert "SQUIBB" in label["openfda"]["manufacturer_name"][0].upper()
    assert " ".join(label["spl_product_data_elements"][:1]).upper().startswith("ELIQUIS APIXABAN")
    assert label["openfda"]["brand_name"][0].upper() == "ELIQUIS", label["openfda"]["brand_name"]
    assert label["openfda"]["route"], "route is blank"


@pytest.mark.live
@pytest.mark.anyio
async def test_live_metformin_defaults_to_immediate_release():
    result = await search_drug_label("metformin")
    maker = result["label"]["openfda"].get("manufacturer_name")
    assert result["release"] == "immediate", f"got modified release from {maker}"


@pytest.mark.live
@pytest.mark.anyio
async def test_live_metformin_er_returns_extended_release():
    result = await search_drug_label("metformin ER")
    assert result["matched_name"] == "METFORMIN HYDROCHLORIDE"
    assert result["release"] != "immediate"


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
