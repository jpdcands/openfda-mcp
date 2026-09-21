from dotenv import load_dotenv
import os
import httpx
from fastmcp import FastMCP

load_dotenv()
API_KEY = os.getenv("OPENFDA_API_KEY")


mcp = FastMCP("OpenFDA MCP Server")

@mcp.tool()
async def get_label_section(drug_name: str, section: str) -> str:
    """
    Pull a specific section (e.g. 'warnings', 'dosage_and_administration',
    'contraindications') from a drug's FDA label.
    """
    url = "https://api.fda.gov/drug/label.json"
    params = {
        "search": f'openfda.brand_name:"{drug_name}"',
        "limit": 1,
        "api_key": API_KEY,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    results = data.get("results", [])
    if not results:
        return f"No label found for '{drug_name}'."

    section_data = results[0].get(section)
    if not section_data:
        return f"No '{section}' section found for '{drug_name}'."

    return "\n".join(section_data)


if __name__ == "__main__":
    mcp.run()