"""Turn raw FAERS adverse event reports into something a reader can use.

A raw report can run to 50,000+ characters, almost all of it OpenFDA tags
attached to the OTHER drugs the patient was on (one aspirin entry carried
664 package NDCs and 161 manufacturer names). The coded fields are also
unreadable as they stand ("patientsex": "2", "reactionoutcome": "5").

This module keeps what a pharmacist reads a report for and decodes it.
Codes follow the FDA's FAERS / ICH E2B field definitions.
"""

DRUG_ROLE = {"1": "suspect", "2": "concomitant", "3": "interacting"}

REACTION_OUTCOME = {
    "1": "recovered/resolved",
    "2": "recovering/resolving",
    "3": "not recovered/not resolved",
    "4": "recovered with sequelae",
    "5": "fatal",
    "6": "unknown",
}

SEX = {"0": "unknown", "1": "male", "2": "female"}

AGE_UNIT = {"800": "decades", "801": "years", "802": "months",
            "803": "weeks", "804": "days", "805": "hours"}

REPORTER = {"1": "physician", "2": "pharmacist", "3": "other health professional",
            "4": "lawyer", "5": "consumer or non-health professional"}

REPORT_TYPE = {"1": "spontaneous", "2": "from a study", "3": "other",
               "4": "not available to sender"}

ACTION_TAKEN = {"1": "drug withdrawn", "2": "dose reduced", "3": "dose increased",
                "4": "dose not changed", "5": "unknown", "6": "not applicable"}

SERIOUSNESS = [
    ("seriousnessdeath", "death"),
    ("seriousnesslifethreatening", "life-threatening"),
    ("seriousnesshospitalization", "hospitalization"),
    ("seriousnessdisabling", "disability"),
    ("seriousnesscongenitalanomali", "congenital anomaly"),
    ("seriousnessother", "other serious condition"),
]

CAUTION = (
    "FAERS reports are submitted by manufacturers, clinicians and the public. "
    "They are not verified, may be duplicated, and do not show that a drug "
    "caused the reaction. Counts are reports, not incidence. Summary counts "
    "cover every report that names this drug where at least one drug is "
    "marked suspect; the individual reports shown are ones where this drug "
    "itself is suspect."
)

MAX_OTHER_DRUGS = 15


def _date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return yyyymmdd
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _drop_empty(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _drug_names(drug: dict) -> list[str]:
    names = [drug.get("medicinalproduct", "")]
    names.append(drug.get("activesubstance", {}).get("activesubstancename", ""))
    return [n for n in names if n]


def names_match(drug: dict, query: str) -> bool:
    """True if this drug entry is the drug the user asked about, by product
    name or active ingredient (so "apixaban" also finds "ELIQUIS")."""
    q = query.lower()
    return any(q in n.lower() for n in _drug_names(drug))


def is_suspect_for(report: dict, query: str) -> bool:
    """True if the queried drug itself is marked suspect (or interacting)."""
    return any(
        names_match(d, query) and d.get("drugcharacterization") in ("1", "3")
        for d in report.get("patient", {}).get("drug", [])
    )


def compact_report(report: dict) -> dict:
    """One FAERS report, decoded and trimmed to what a reader needs."""
    patient = report.get("patient", {})

    age = patient.get("patientonsetage")
    unit = AGE_UNIT.get(patient.get("patientonsetageunit", ""), "")
    weight = patient.get("patientweight")

    suspects, others = [], []
    for d in patient.get("drug", []):
        role = DRUG_ROLE.get(d.get("drugcharacterization", ""), "unknown")
        name = d.get("medicinalproduct", "?")
        if role in ("suspect", "interacting"):
            suspects.append(_drop_empty({
                "name": name,
                "role": role,
                "active_ingredient": d.get("activesubstance", {}).get("activesubstancename"),
                "dose": d.get("drugdosagetext"),
                "indication": d.get("drugindication"),
                "action_taken": ACTION_TAKEN.get(d.get("actiondrug", "")),
            }))
        else:
            others.append(name)

    serious = [label for field, label in SERIOUSNESS if report.get(field) == "1"]

    return _drop_empty({
        "report_id": report.get("safetyreportid"),
        "received": _date(report.get("receivedate")),
        "country": report.get("occurcountry") or report.get("primarysourcecountry"),
        "reporter": REPORTER.get(report.get("primarysource", {}).get("qualification", "")),
        "report_type": REPORT_TYPE.get(report.get("reporttype", "")),
        "seriousness": serious or ["not serious"],
        "patient": _drop_empty({
            "age": f"{age} {unit}".strip() if age else None,
            "sex": SEX.get(patient.get("patientsex", "")),
            "weight_kg": weight,
        }),
        "reactions": [
            _drop_empty({
                "term": r.get("reactionmeddrapt"),
                "outcome": REACTION_OUTCOME.get(r.get("reactionoutcome", "")),
            })
            for r in patient.get("reaction", [])
        ],
        "suspect_drugs": suspects,
        "other_drugs": others[:MAX_OTHER_DRUGS]
        + ([f"...and {len(others) - MAX_OTHER_DRUGS} more"] if len(others) > MAX_OTHER_DRUGS else []),
    })


def summarize_counts(total: int, serious_terms: list[dict],
                     death_terms: list[dict], reaction_terms: list[dict]) -> dict:
    """Build the overview from OpenFDA 'count' results."""
    serious = next((t["count"] for t in serious_terms if str(t["term"]) == "1"), 0)
    deaths = next((t["count"] for t in death_terms if str(t["term"]) == "1"), 0)
    return {
        "total_reports": total,
        "serious_reports": serious,
        "reports_with_death": deaths,
        "top_reactions": [
            {"term": t["term"], "reports": t["count"]} for t in reaction_terms[:10]
        ],
    }
