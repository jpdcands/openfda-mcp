import asyncio
import os
import re

import httpx

from faers_format import is_suspect_for, summarize_counts


BASE_URL = "https://api.fda.gov"
API_KEY = os.getenv("OPENFDA_API_KEY")  # optional; raises the daily rate limit
DEFAULT_PARAMS = {"api_key": API_KEY} if API_KEY else {}
LABEL_URL = f"{BASE_URL}/drug/label.json"
NDC_URL = f"{BASE_URL}/drug/ndc.json"


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
    return await _get_url(client, LABEL_URL, params)


async def _get_url(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    response = await client.get(url, params=params)
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
    "QUALLENT", "QPHARMA", "QUALITY CARE", "READYMEDS", "SAFECOR",
    "ST. MARY'S MEDICAL PARK", "UNIT DOSE SERVICES",
]


def is_repackager_name(name: str) -> bool:
    n = name.upper()
    return any(m in n for m in REPACKAGER_MARKERS)


def is_repackager(label: dict) -> bool:
    names = label.get("openfda", {}).get("manufacturer_name", [])
    return any(is_repackager_name(n) for n in names)


# ---------------------------------------------------------------------------
# Immediate release vs extended release
# ---------------------------------------------------------------------------

# Words a user might type to ask for a modified-release product.
RELEASE_WORDS = {"ER", "XR", "XL", "SR", "CR", "LA", "CD", "DR", "EC",
                 "EXTENDED-RELEASE", "EXTENDED", "DELAYED-RELEASE", "DELAYED",
                 "SUSTAINED-RELEASE", "CONTROLLED-RELEASE", "RELEASE"}

# Phrases that mark a label as modified release.
MODIFIED_RELEASE_PHRASES = [
    "extended-release", "extended release", "delayed-release",
    "delayed release", "sustained-release", "sustained release",
    "controlled-release", "controlled release", "modified-release",
]


def split_release_request(drug_name: str) -> tuple[str, bool]:
    """'metformin ER' -> ('metformin', True); 'metformin' -> ('metformin', False)."""
    words = drug_name.split()
    kept = [w for w in words if w.upper() not in RELEASE_WORDS]
    wants_mr = len(kept) < len(words)
    return (" ".join(kept) or drug_name), wants_mr


def is_modified_release(label: dict) -> bool:
    """True if the label is for an extended/delayed/sustained-release product.

    Looks only at the product names and the indications section; other
    sections of an immediate-release label often mention ER products
    (e.g. "patients switching from extended-release tablets").
    """
    ofda = label.get("openfda", {})
    text = " ".join(
        ofda.get("brand_name", [])
        + label.get("spl_product_data_elements", [])[:1]
        + label.get("indications_and_usage", [])[:1]
    ).lower()
    return any(p in text for p in MODIFIED_RELEASE_PHRASES)


def pick_label(results: list[dict], wants_mr: bool = False) -> dict:
    """Choose one label. In order of importance:
      1. from an original manufacturer, not a repackager
      2. the release type the user asked for (immediate release by default)
      3. the newest
    """
    return max(results, key=lambda r: (
        not is_repackager(r),
        is_modified_release(r) == wants_mr,
        r.get("effective_time", ""),
    ))


def choose_manufacturers(maker_terms: list[dict], limit: int = 10) -> list[str]:
    """From an OpenFDA manufacturer count, keep original manufacturers
    (most labels first). Empty if every one is a repackager."""
    originals = [t for t in maker_terms if not is_repackager_name(t["term"])]
    originals.sort(key=lambda t: -t["count"])
    return [t["term"] for t in originals[:limit]]


def _quote(value: str) -> str:
    return '"' + value.replace('"', "") + '"'


# ---------------------------------------------------------------------------
# Untagged labels: identify the company by the NDC printed on the carton
# ---------------------------------------------------------------------------
#
# Some labels, including brand manufacturers' own (e.g. Bristol-Myers
# Squibb's ELIQUIS), carry no OpenFDA tags, so a brand-name search only finds
# repackagers. The label text can't be trusted to name the company either:
# repackagers copy "Marketed by: Bristol-Myers Squibb" word for word. What
# they can't copy is the NDC on their own carton. So:
#   1. the FDA NDC directory says which labeler code belongs to which company
#   2. drop the repackagers -> the original company's code(s), e.g. 0003
#   3. pick the label whose carton (principal display panel) shows that code

NDC_PATTERN = re.compile(r"\b(\d{4,5})-\d{3,4}-\d{1,2}\b")


def panel_labeler_codes(label: dict) -> set[str]:
    """Labeler codes of the NDCs printed on the label's carton."""
    panel = " ".join(label.get("package_label_principal_display_panel", []))
    return set(NDC_PATTERN.findall(panel))


def original_ndc_products(products: list[dict], name: str = "") -> dict[str, dict]:
    """NDC directory entries -> {labeler code: entry} for non-repackagers.

    A company often has several entries for one drug (e.g. ELIQUIS tablets,
    ELIQUIS 30-Day Starter Pack, ELIQUIS SPRINKLE). The entry used to describe
    the label is the one whose brand or generic name is exactly what was
    searched for, preferring one that lists a route.
    """
    def fit(p: dict) -> tuple:
        exact = name.upper() in (p.get("brand_name", "").upper(),
                                 p.get("generic_name", "").upper())
        return (exact, bool(p.get("route")))

    found = {}
    for p in products:
        code = p.get("product_ndc", "").split("-")[0]
        if not code or is_repackager_name(p.get("labeler_name", "")):
            continue
        if code not in found or fit(p) > fit(found[code]):
            found[code] = p
    return found


def is_plain_product(label: dict, brand: str, generic: str) -> bool:
    """True for 'ELIQUIS apixaban ...', False for variants such as
    'ELIQUIS SPRINKLE apixaban ...' filed as a separate label."""
    first = " ".join(label.get("spl_product_data_elements", [])[:1]).upper()
    return first.startswith(f"{brand} {generic}".upper())


def pick_untagged_original(results: list[dict], originals: dict[str, dict],
                           wants_mr: bool = False) -> dict | None:
    """Among labels found by text search, choose one whose carton NDC belongs
    to an original company. Returns a copy with OpenFDA-style tags filled in
    from the NDC directory, or None if no carton matches."""
    candidates = []
    for r in results:
        codes = panel_labeler_codes(r) & originals.keys()
        if codes:
            candidates.append((r, originals[sorted(codes)[0]]))
    if not candidates:
        return None

    def rank(item):
        label, product = item
        return (
            is_plain_product(label, product.get("brand_name", ""),
                             product.get("generic_name", "")),
            is_modified_release(label) == wants_mr,
            label.get("effective_time", ""),
        )

    label, product = max(candidates, key=rank)
    label = dict(label)
    label["openfda"] = {
        "brand_name": [product.get("brand_name", "")],
        "generic_name": [product.get("generic_name", "").upper()],
        "manufacturer_name": [product.get("labeler_name", "")],
        "route": product.get("route", []),
    }
    return label


async def search_drug_label(drug_name: str) -> dict:
    """Find the FDA label that best matches drug_name.

    Returns {"label", "match", "matched_name", "label_source",
             "release", "other_names"}.
    """
    name, wants_mr = split_release_request(drug_name)

    async with httpx.AsyncClient(params=DEFAULT_PARAMS, timeout=30) as client:
        brand_terms, generic_terms = await asyncio.gather(
            _count(client, "brand_name", name),
            _count(client, "generic_name", name),
        )
        choice = choose_term(name, brand_terms, generic_terms)

        if choice:
            field, term, match = choice
            search = f"{field}:{_quote(term)}"
        else:
            # Nothing matched cleanly; fall back to the broad search and say so.
            term, match = name, "approximate (no exact or single-ingredient match)"
            search = (
                f"openfda.brand_name:{_quote(name)}"
                f" OR openfda.generic_name:{_quote(name)}"
            )

        # Which companies have a label for this product? Ask for the original
        # manufacturers' labels directly, so repackagers can't crowd them out.
        makers = await _get(client, {
            "search": search, "count": "openfda.manufacturer_name.exact",
        })
        originals = choose_manufacturers(makers.get("results", []))
        if originals:
            wanted = " OR ".join(
                f"openfda.manufacturer_name.exact:{_quote(m)}" for m in originals
            )
            search = f"({search}) AND ({wanted})"

        data = await _get(client, {"search": search, "limit": 25})

        # Every tagged label is a repackager's: look for the original
        # company's untagged label, identified by the NDC on its carton.
        untagged = None
        if choice and not originals:
            ndc_field = "brand_name" if field.endswith("brand_name.exact") else "generic_name"
            ndc = await _get_url(client, NDC_URL, {
                "search": f"{ndc_field}:{_quote(term)}", "limit": 100,
            })
            ndc_originals = original_ndc_products(ndc.get("results", []), term)
            if ndc_originals:
                text_hits = await _get(client, {
                    "search": f"spl_product_data_elements:{_quote(term)}",
                    "limit": 25,
                })
                untagged = pick_untagged_original(
                    text_hits.get("results", []), ndc_originals, wants_mr
                )

    # Other generic names that also matched, most labels first, so the
    # caller can see what else exists (combinations, ER products, ...).
    others = [
        f'{t["term"]} ({t["count"]} labels)'
        for t in sorted(generic_terms, key=lambda t: -t["count"])
        if t["term"] != term
    ][:5]

    if untagged:
        label, source = untagged, "manufacturer (untagged label, matched by carton NDC)"
    else:
        results = data.get("results", [])
        if not results:
            return {"label": None, "match": None, "matched_name": None,
                    "other_names": others}
        label = pick_label(results, wants_mr)
        source = "repackager" if is_repackager(label) else "manufacturer"

    return {
        "label": label, "match": match, "matched_name": term,
        "label_source": source,
        "release": "modified (ER/DR/SR)" if is_modified_release(label) else "immediate",
        "other_names": others,
    }


EVENT_URL = f"{BASE_URL}/drug/event.json"


def adverse_event_search(drug_name: str) -> str:
    """Reports naming the drug (by product name or active ingredient) in
    which at least one drug is marked suspect."""
    q = _quote(drug_name)
    return (
        f"(patient.drug.medicinalproduct:{q}"
        f" OR patient.drug.activesubstance.activesubstancename:{q})"
        " AND patient.drug.drugcharacterization:1"
    )


async def search_adverse_events(drug_name: str, limit: int = 5) -> dict:
    """Overview counts plus the newest reports in which THIS drug is suspect.

    Returns {"summary": {...}, "reports": [raw FAERS reports]}.
    """
    search = adverse_event_search(drug_name)

    async def count(field: str, n: int = 10) -> list[dict]:
        data = await _get_url(client, EVENT_URL,
                              {"search": search, "count": field, "limit": n})
        return data.get("results", [])

    async with httpx.AsyncClient(params=DEFAULT_PARAMS, timeout=30) as client:
        newest, serious, deaths, reactions = await asyncio.gather(
            _get_url(client, EVENT_URL, {
                "search": search, "sort": "receivedate:desc", "limit": 50,
            }),
            count("serious"),
            count("seriousnessdeath"),
            count("patient.reaction.reactionmeddrapt.exact"),
        )

    total = newest.get("meta", {}).get("results", {}).get("total", 0)
    # The search can't tie "suspect" to the same drug entry as the name, so
    # keep only reports where the queried drug ITSELF is marked suspect.
    kept = [r for r in newest.get("results", []) if is_suspect_for(r, drug_name)]
    return {
        "summary": summarize_counts(total, serious, deaths, reactions),
        "reports": kept[:limit],
    }
