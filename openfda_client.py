import asyncio
import os

import httpx


BASE_URL = "https://api.fda.gov"
API_KEY = os.getenv("OPENFDA_API_KEY")  # optional; raises the daily rate limit
DEFAULT_PARAMS = {"api_key": API_KEY} if API_KEY else {}
LABEL_URL = f"{BASE_URL}/drug/label.json"


# ---------------------------------------------------------------------------
# Step 1: work out WHICH product the user means
# ---------------------------------------------------------------------------

def is_combination(term: str) -> bool:
    """OpenFDA joins combination products with ' AND ' or commas,
    e.g. 'SITAGLIPTIN AND METFORMIN HYDROCHLORIDE'."""
    t = term.upper()
    return " AND " in t or ", " in t


def choose_term(query: str, brand_terms: list[dict], generic_terms: list[dict]):
    """Pick the exact OpenFDA name that best matches what the user typed.

    brand_terms / generic_terms are OpenFDA 'count' results:
        [{"term": "METFORMIN HYDROCHLORIDE", "count": 261}, ...]

    Returns (field, term, match) or None. Order of preference:
      1. single-ingredient generic name starting with (or equal to) what
         was typed; the name on the most labels wins
                                                   metformin -> METFORMIN HYDROCHLORIDE
      2. brand name is exactly what was typed      Glucophage -> GLUCOPHAGE
    Generic names are checked before brand names because many generic
    products file their label with the generic as the "brand"
    (e.g. brand "Metformin" on a label whose generic is "METFORMIN ER 500 MG").
    Combination products are only chosen if the user typed the combination.
    """
    q = query.upper().strip()

    # Every single-ingredient generic name that starts with what was typed,
    # including an exact match. The one used on the MOST labels wins, because
    # OpenFDA data has stray one-off entries (a label whose generic is just
    # "METFORMIN", or "METFORMIN ER 500 MG") that must not beat the standard
    # name used by hundreds of labels ("METFORMIN HYDROCHLORIDE").
    singles = [
        t for t in generic_terms
        if t["term"].upper().startswith(q)
        and (not is_combination(t["term"]) or t["term"].upper() == q)
    ]
    if singles:
        best = max(singles, key=lambda t: t["count"])
        how = "exact generic name" if best["term"].upper() == q else "generic name (salt form)"
        return ("openfda.generic_name.exact", best["term"], how)

    for t in brand_terms:
        if t["term"].upper() == q:
            return ("openfda.brand_name.exact", t["term"], "exact brand name")

    return None


async def _get(client: httpx.AsyncClient, params: dict) -> dict:
    response = await client.get(LABEL_URL, params=params)
    # OpenFDA answers "no matches" with a 404 rather than an empty list.
    if response.status_code == 404:
        return {"results": []}
    response.raise_for_status()
    return response.json()


async def _count(client: httpx.AsyncClient, field: str, drug_name: str) -> list[dict]:
    data = await _get(client, {
        "search": f'openfda.{field}:"{drug_name}"',
        "count": f"openfda.{field}.exact",
    })
    return data.get("results", [])


# ---------------------------------------------------------------------------
# Step 2: fetch the label for that product
# ---------------------------------------------------------------------------

# Repackagers and relabelers file their own copy of a manufacturer's label.
# The clinical text is the same, but a pharmacist expects the original
# manufacturer's label, so these are used only when nothing else exists.
# Matched as upper-case substrings of openfda.manufacturer_name.
REPACKAGER_MARKERS = [
    "REPACK", "PREPACK", "RELABEL",
    "A-S MEDICATION", "AMERICAN HEALTH PACKAGING", "APHENA", "ASCLEMED",
    "AVKARE", "AVPAK", "BRYANT RANCH", "CARDINAL HEALTH", "COUPLER",
    "DENTON PHARMA", "DIRECT RX", "DIRECT_RX", "GOLDEN STATE MEDICAL",
    "HENRY SCHEIN", "LAKE ERIE MEDICAL", "MAJOR PHARMACEUTICALS",
    "MEDSOURCE", "MEDVANTX", "NORTHWIND", "NUCARE", "PD-RX",
    "PRECISION DOSE", "PREFERRED PHARMACEUTICALS", "PROFICIENT RX",
    "QPHARMA", "QUALITY CARE", "READYMEDS", "SAFECOR",
    "ST. MARY'S MEDICAL PARK", "UNIT DOSE SERVICES",
]


def is_repackager(label: dict) -> bool:
    names = label.get("openfda", {}).get("manufacturer_name", [])
    return any(m in n.upper() for n in names for m in REPACKAGER_MARKERS)


def pick_label(results: list[dict]) -> dict:
    """Newest label from an original manufacturer; if every label is from a
    repackager, the newest of those."""
    originals = [r for r in results if not is_repackager(r)]
    pool = originals or results
    return max(pool, key=lambda r: r.get("effective_time", ""))


async def search_drug_label(drug_name: str) -> dict:
    """Find the FDA label that best matches drug_name.

    Returns {"label": <raw label or None>, "match": <how it matched>,
             "matched_name": <the OpenFDA name used>}.
    """
    async with httpx.AsyncClient(params=DEFAULT_PARAMS, timeout=30) as client:
        brand_terms, generic_terms = await asyncio.gather(
            _count(client, "brand_name", drug_name),
            _count(client, "generic_name", drug_name),
        )
        choice = choose_term(drug_name, brand_terms, generic_terms)

        if choice:
            field, term, match = choice
            search = f'{field}:"{term}"'
        else:
            # Nothing matched cleanly; fall back to the broad search and say so.
            term, match = drug_name, "approximate (no exact or single-ingredient match)"
            search = (
                f'openfda.brand_name:"{drug_name}"'
                f' OR openfda.generic_name:"{drug_name}"'
            )

        data = await _get(client, {"search": search, "limit": 25})

    # Other generic names that also matched, most labels first, so the
    # caller can see what else exists (combinations, ER products, ...).
    others = [
        f'{t["term"]} ({t["count"]} labels)'
        for t in sorted(generic_terms, key=lambda t: -t["count"])
        if t["term"] != term
    ][:5]

    results = data.get("results", [])
    if not results:
        return {"label": None, "match": None, "matched_name": None,
                "other_names": others}

    label = pick_label(results)
    return {"label": label, "match": match, "matched_name": term,
            "label_source": "repackager" if is_repackager(label) else "manufacturer",
            "other_names": others}


async def search_adverse_events(drug_name: str, limit: int = 5) -> dict:
    """Search FAERS for reports where drug_name is the suspect drug."""
    async with httpx.AsyncClient(params=DEFAULT_PARAMS) as client:
        response = await client.get(
            f"{BASE_URL}/drug/event.json",
            params={
                "search": (
                    f'patient.drug.medicinalproduct:"{drug_name}"'
                    ' AND patient.drug.drugcharacterization:1'
                ),
                "limit": 100,
            },
        )
        response.raise_for_status()
        data = response.json()

    # Double-check: keep a report only if THIS drug is marked suspect (1)
    name = drug_name.lower()
    kept = []
    for report in data.get("results", []):
        for drug in report.get("patient", {}).get("drug", []):
            if (
                name in drug.get("medicinalproduct", "").lower()
                and drug.get("drugcharacterization") == "1"
            ):
                kept.append(report)
                break

    data["results"] = kept[:limit]
    return data
