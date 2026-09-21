from fastmcp import FastMCP
from openfda_client import search_drug_label, search_adverse_events
import os

mcp = FastMCP("OpenFDA MCP Server")

@mcp.tool()
async def get_drug_label(drug_name: str) -> dict:
    """
    Look up FDA label information for a drug by brand or generic name.
    Returns indications, warnings, dosage, and contraindications.
    """
    result = await search_drug_label(drug_name)
    if not result.get("results"):
        return {"error": f"No label found for '{drug_name}'"}
    return result["results"][0]

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
