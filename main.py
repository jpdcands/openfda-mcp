from fastmcp import FastMCP
from openfda_client import search_drug_label, search_adverse_events
from label_format import summarize_label
from faers_format import CAUTION, compact_report
import os

mcp = FastMCP("OpenFDA MCP Server")

@mcp.tool()
async def get_drug_label(drug_name: str, sections: list[str] | None = None) -> dict:
    """
    Look up the FDA label for a drug by brand or generic name.

    Plain generic names match the single-ingredient product
    (e.g. "metformin" -> metformin hydrochloride, not a combination).
    Type the combination to get one (e.g. "sitagliptin and metformin").
    Immediate-release labels are preferred; add ER, XR, DR, SR etc. to ask
    for a modified-release product (e.g. "metformin ER").
    Original manufacturers' labels are preferred over repackagers'.

    By default returns boxed warning, indications, dosage, contraindications
    and warnings, each capped in length. Pass `sections` to ask for others:
    boxed_warning, indications_and_usage, dosage_and_administration,
    contraindications, warnings_and_cautions, warnings,
    dosage_forms_and_strengths, adverse_reactions, drug_interactions,
    use_in_specific_populations, pregnancy, lactation, pediatric_use,
    geriatric_use, renal_impairment, hepatic_impairment, overdosage,
    mechanism_of_action, pharmacokinetics, how_supplied, storage_and_handling.
    """
    result = await search_drug_label(drug_name)
    if result["label"] is None:
        return {"error": f"No label found for '{drug_name}'"}
    summary = summarize_label(result["label"], sections)
    return {"query": drug_name, "match": result["match"],
            "matched_name": result["matched_name"],
            "label_source": result["label_source"],
            "release": result["release"], **summary,
            "other_names": result["other_names"]}

@mcp.tool()
async def get_adverse_events(drug_name: str, limit: int = 5) -> dict:
    """
    Summarize FAERS adverse event reports for a drug (brand name, generic
    name or active ingredient).

    Returns an overview across ALL matching reports (total, serious, deaths,
    top 10 reactions) plus the newest individual reports (default 5, max 10)
    in which this drug is marked suspect, decoded into plain terms.
    FAERS reports are unverified and do not establish causation.
    """
    limit = max(1, min(limit, 10))
    result = await search_adverse_events(drug_name, limit)
    if not result["summary"]["total_reports"]:
        return {"error": f"No adverse event reports found for '{drug_name}'"}
    return {
        "drug": drug_name,
        "caution": CAUTION,
        "summary": result["summary"],
        "newest_reports": [compact_report(r) for r in result["reports"]],
    }

if __name__ == "__main__":
    if os.getenv("MCP_TRANSPORT") == "http":
        mcp.run(
            transport="http",
            host="127.0.0.1",
            port=int(os.getenv("PORT", "8001")),
        )
    else:
        mcp.run()
