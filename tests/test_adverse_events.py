"""Tests for get_adverse_events.

Fast tests (default):   uv run pytest
Live API tests:         uv run pytest -m live
"""
import json
from pathlib import Path

import pytest
from fastmcp import Client

import main
from faers_format import compact_report, is_suspect_for, summarize_counts
from openfda_client import search_adverse_events

CLIENT_LIMIT_CHARS = 100_000

# Two real FAERS reports returned for "Eliquis" (Sep 2026). The second is
# 51,000 characters, almost all OpenFDA tags on a co-reported aspirin.
REAL = json.loads(
    (Path(__file__).parent / "fixtures" / "faers_eliquis_2_reports.json").read_text()
)
JAPAN_DEATH, US_HAEMATURIA = REAL


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- Decoding one report -----------------------------------------------------

def test_big_report_shrinks_to_a_readable_size():
    assert len(json.dumps(US_HAEMATURIA)) > 50_000
    assert len(json.dumps(compact_report(US_HAEMATURIA))) < 1_500


def test_codes_are_decoded():
    r = compact_report(JAPAN_DEATH)
    assert r["received"] == "2014-03-12"
    assert r["reporter"] == "physician"
    assert r["seriousness"] == ["death"]
    assert r["patient"] == {"age": "70 years", "sex": "female", "weight_kg": "85.9"}
    assert r["reactions"] == [{"term": "Death", "outcome": "fatal"}]


def test_suspect_and_other_drugs_are_separated():
    r = compact_report(JAPAN_DEATH)
    assert [d["name"] for d in r["suspect_drugs"]] == ["SELARA", "ELIQUIS"]
    eliquis = r["suspect_drugs"][1]
    assert eliquis["dose"] == "5 MG, 2X/DAY"
    assert eliquis["action_taken"] == "unknown"
    assert r["other_drugs"] == ["MAINTATE", "EQUA", "URSO", "AZILVA", "FEBURIC"]


def test_indication_and_reporter_type():
    r = compact_report(US_HAEMATURIA)
    assert r["suspect_drugs"][0]["indication"] == "ATRIAL FIBRILLATION"
    assert r["reporter"] == "consumer or non-health professional"
    assert r["seriousness"] == ["other serious condition"]
    assert r["other_drugs"] == ["ASPIRIN."]


def test_long_other_drug_lists_are_capped():
    many = {"patient": {"drug": [{"medicinalproduct": f"DRUG{i}",
                                  "drugcharacterization": "2"} for i in range(40)]}}
    others = compact_report(many)["other_drugs"]
    assert len(others) == 16 and others[-1] == "...and 25 more"


# --- Is THIS drug the suspect? ----------------------------------------------

def test_suspect_check_uses_name_or_active_ingredient():
    assert is_suspect_for(JAPAN_DEATH, "eliquis")
    assert is_suspect_for(JAPAN_DEATH, "apixaban")


def test_concomitant_only_is_not_suspect():
    # Aspirin appears in the US report, but only as a concomitant drug.
    assert not is_suspect_for(US_HAEMATURIA, "aspirin")


# --- Overview counts ---------------------------------------------------------

def test_summary_counts():
    s = summarize_counts(
        1200,
        [{"term": 1, "count": 900}, {"term": 2, "count": 300}],
        [{"term": 1, "count": 45}],
        [{"term": "Haemorrhage", "count": 150}, {"term": "Death", "count": 45}],
    )
    assert s == {"total_reports": 1200, "serious_reports": 900,
                 "reports_with_death": 45,
                 "top_reactions": [{"term": "Haemorrhage", "reports": 150},
                                   {"term": "Death", "reports": 45}]}


# --- Through a real MCP client -------------------------------------------------

@pytest.mark.anyio
async def test_tool_output_fits_and_carries_caution(monkeypatch):
    async def fake(name, limit):
        return {"summary": summarize_counts(2, [], [], []), "reports": REAL * 5}

    monkeypatch.setattr(main, "search_adverse_events", fake)
    async with Client(main.mcp) as client:
        result = await client.call_tool("get_adverse_events",
                                        {"drug_name": "Eliquis", "limit": 10})
    text = result.content[0].text
    assert len(text) < 20_000
    data = json.loads(text)
    assert "do not show that a drug caused" in data["caution"]
    assert "openfda" not in text


# --- Against the real OpenFDA API ------------------------------------------

@pytest.mark.live
@pytest.mark.anyio
@pytest.mark.parametrize("drug", ["Eliquis", "apixaban", "metformin", "lisinopril"])
async def test_live_adverse_events(drug):
    async with Client(main.mcp) as client:
        result = await client.call_tool("get_adverse_events", {"drug_name": drug})
    text = result.content[0].text
    data = json.loads(text)
    assert "error" not in data, data
    assert len(text) < CLIENT_LIMIT_CHARS
    assert data["summary"]["total_reports"] > 100
    assert data["summary"]["top_reactions"]
    dates = [r["received"] for r in data["newest_reports"]]
    assert dates, "no individual reports kept"
    assert dates == sorted(dates, reverse=True), "reports are not newest first"
    assert dates[0] >= "2024", f"newest report is old: {dates[0]}"


@pytest.mark.live
@pytest.mark.anyio
async def test_live_every_kept_report_has_the_drug_as_suspect():
    result = await search_adverse_events("Eliquis", 10)
    assert result["reports"]
    for r in result["reports"]:
        assert is_suspect_for(r, "Eliquis") or is_suspect_for(r, "apixaban")
