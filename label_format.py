"""Turn a raw OpenFDA label into a compact answer an MCP client can use.

A raw label can run to 100,000+ characters (it includes every section plus
HTML copies of every table), which overflows a client's tool-output limit.
This module keeps only the sections asked for and caps each one.
"""

# Sections returned when the caller doesn't ask for specific ones.
DEFAULT_SECTIONS = [
    "boxed_warning",
    "indications_and_usage",
    "dosage_and_administration",
    "contraindications",
    "warnings_and_cautions",
    "warnings",  # older labels use this instead of warnings_and_cautions
]

# Everything a caller may ask for by name.
ALLOWED_SECTIONS = DEFAULT_SECTIONS + [
    "dosage_forms_and_strengths",
    "adverse_reactions",
    "drug_interactions",
    "use_in_specific_populations",
    "pregnancy",
    "lactation",
    "pediatric_use",
    "geriatric_use",
    "renal_impairment",
    "hepatic_impairment",
    "overdosage",
    "mechanism_of_action",
    "pharmacokinetics",
    "how_supplied",
    "storage_and_handling",
]

MAX_SECTION_CHARS = 3000   # per section
MAX_TOTAL_CHARS = 20000    # whole response, roughly 5,000 tokens


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …[truncated]", True


def summarize_label(label: dict, sections: list[str] | None = None) -> dict:
    """Build the compact response for one label."""
    wanted = sections or DEFAULT_SECTIONS
    unknown = [s for s in wanted if s not in ALLOWED_SECTIONS]
    wanted = [s for s in wanted if s in ALLOWED_SECTIONS]

    ofda = label.get("openfda", {})
    out = {
        "brand_name": ofda.get("brand_name", []),
        "generic_name": ofda.get("generic_name", []),
        "manufacturer": ofda.get("manufacturer_name", []),
        "route": ofda.get("route", []),
        "effective_date": label.get("effective_time"),
        "set_id": label.get("set_id"),
        "sections": {},
        "truncated_sections": [],
        "missing_sections": [],
    }
    if unknown:
        out["ignored_sections"] = unknown

    budget = MAX_TOTAL_CHARS
    for name in wanted:
        # Fields ending in _table are HTML duplicates; we never read them.
        raw = label.get(name)
        if not raw:
            if name != "warnings":  # only one of the two warnings fields exists
                out["missing_sections"].append(name)
            continue
        text = " ".join(raw).strip()
        limit = min(MAX_SECTION_CHARS, budget)
        if limit <= 0:
            out["truncated_sections"].append(name)
            continue
        text, cut = _clip(text, limit)
        out["sections"][name] = text
        budget -= len(text)
        if cut:
            out["truncated_sections"].append(name)

    if "warnings_and_cautions" in out["sections"]:
        out["missing_sections"] = [
            s for s in out["missing_sections"] if s != "warnings"
        ]
    return out
