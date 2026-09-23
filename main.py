from fastmcp import FastMCP
from openfda_client import search_drug_label, search_adverse_events
from label_format import summarize_label
import os

mcp = FastMCP("OpenFDA MCP Server")

@mcp.tool()
async def get_drug_label(drug_name: str, sections: list[str] | None = None) -> dict:
    """
    Look up the FDA label for a drug by brand or generic name.

    Plain generic names match the single-ingredient product
    (e.g. "metformin" -> metformin hydrochloride, not a combination).
    Type the combination to get one (e.g. "sitagliptin and metformin").

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
            "label_source": result["label_source"], **summary,
            "other_names": result["other_names"]}

@mcp.tool()
async def get_adverse_events(drug_name: str, limit: int = 5) -> dict:
    """
    Retrieve recent adverse event reports for a drug from FAERS.
    """
    result = await search_adverse_events(drug_name, limit)
    if not result.get("results"):
        return {"error": f"No adverse event reports found for '{drug_name}'"}
    return {
        "drug": drug_name,
        "count": len(result["results"]),
        "results": result["results"],
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
