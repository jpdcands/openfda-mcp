import os

import httpx


BASE_URL = "https://api.fda.gov"
API_KEY = os.getenv("OPENFDA_API_KEY")  # optional; raises the daily rate limit
DEFAULT_PARAMS = {"api_key": API_KEY} if API_KEY else {}

async def search_drug_label(drug_name: str, limit: int = 5) -> dict:
    """Search the OpenFDA drug label endpoint by brand or generic name."""
    async with httpx.AsyncClient(params=DEFAULT_PARAMS) as client:
        response = await client.get(
            f"{BASE_URL}/drug/label.json",
            params={
                "search": f'openfda.brand_name:"{drug_name}" OR openfda.generic_name:"{drug_name}"',
                "limit": limit,
            },
        )
        response.raise_for_status()
        return response.json()

async def search_adverse_events(drug_name: str, limit: int = 5) -> dict:
    """Search the OpenFDA adverse event (FAERS) endpoint by drug name."""
    async with httpx.AsyncClient(params=DEFAULT_PARAMS) as client:
        response = await client.get(
            f"{BASE_URL}/drug/event.json",
            params={
                "search": f'patient.drug.medicinalproduct:"{drug_name}"',
                "limit": limit,
            },
        )
        response.raise_for_status()
        return response.json()