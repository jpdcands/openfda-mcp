# OpenFDA MCP Server

A [Model Context Protocol](https://modelcontextprotocol.io) (MCP) server that gives AI assistants like Claude live access to FDA drug labeling and adverse event data through the public [openFDA API](https://open.fda.gov/apis/).

Built by a retired pharmacist (RPh) with 32 years of practice. The tools don't just relay raw API output: they pick the label a pharmacist would actually want, and they trim and decode the data so it's readable.

---

## Why this exists

Language models answer drug questions from memory, which can be outdated or wrong. This server lets an assistant look up current FDA data instead. The raw openFDA responses are hard to use as they stand: a single label can exceed 100,000 characters, and an adverse event report can exceed 50,000. This server does the pharmacist's work of choosing the right product and reducing it to what matters.

## Tools

### `get_drug_label(drug_name, sections=None)`

Looks up the FDA label for a drug by brand or generic name.

By default it returns the boxed warning, indications, dosage and administration, contraindications, and warnings, each capped in length. Pass `sections` to request others, such as `adverse_reactions`, `drug_interactions`, `pregnancy`, `renal_impairment`, `overdosage`, `pharmacokinetics`, or `how_supplied`.

How it chooses the label:

- **Single-ingredient first.** "metformin" returns metformin hydrochloride, not a combination product. Type the combination ("sitagliptin and metformin") to get one.
- **Original manufacturer over repackager.** Repackagers file copies of a manufacturer's label. The server prefers the original and falls back to a repackager only when nothing else exists.
- **Immediate release by default.** Add ER, XR, DR, SR and similar to ask for a modified-release product ("metformin ER").
- **Untagged manufacturer labels.** Some brand labels carry no openFDA tags, so a name search finds only repackagers. For these the server identifies the original company by the NDC printed on the carton, which repackagers can't copy.
- **Transparent matching.** The response states how the name was matched, whether the label came from a manufacturer or a repackager, and lists other matching products.

### `get_adverse_events(drug_name, limit=5)`

Summarizes FDA Adverse Event Reporting System (FAERS) reports for a drug.

It returns an overview across all matching reports (total, serious, deaths, top 10 reactions) plus the newest individual reports, up to 10, in which the drug is marked as suspect. Coded fields are decoded into plain terms, and every response carries a caution that FAERS reports are unverified and don't establish causation.

### Example prompts

- "What's the boxed warning on warfarin?"
- "Pull the contraindications and renal dosing for metformin ER."
- "Summarize the adverse event reports for apixaban."

## Tech stack

- Python 3.11+
- [FastMCP](https://github.com/jlowin/fastmcp) 4.x
- [httpx](https://www.python-httpx.org/) for async calls to openFDA
- [uv](https://docs.astral.sh/uv/) for dependency management
- pytest for tests

## Getting started

### Install

```bash
git clone https://github.com/jpdcands/openfda-mcp.git
cd openfda-mcp
uv sync
```

### Configuration

An openFDA API key is optional but raises your daily rate limit ([get one here](https://open.fda.gov/apis/authentication/)). Set it as an environment variable or in a `.env` file:

```bash
OPENFDA_API_KEY=your_key_here
```

### Run locally (STDIO)

```bash
uv run main.py
```

### Try it with MCP Inspector

```bash
npx @modelcontextprotocol/inspector uv run main.py
```

Select a tool, enter a drug name, and check the response.

### Run as a remote server (HTTP)

```bash
MCP_TRANSPORT=http PORT=8001 uv run main.py
```

This serves the MCP endpoint at `http://127.0.0.1:8001/mcp`. The server binds to localhost only, so put a reverse proxy such as Caddy or nginx in front of it to expose it over HTTPS.

## Connecting to Claude

- **Remote:** run the server over HTTP behind HTTPS and add its `/mcp` URL as a custom connector in Claude Desktop.
- **Local:** add an entry under `mcpServers` in `claude_desktop_config.json` that runs `uv` with the arguments `run main.py` from this project's directory.

## Testing

```bash
uv sync                   # installs pytest too (dev group)
uv run pytest             # fast offline tests, no network
uv run pytest -m live     # also hits the real OpenFDA API
```

Run both before every push. The live tests check that common drugs (metformin, lisinopril, warfarin, ...) return the single-ingredient label and that every response fits inside Claude's 25,000-token tool-output limit.

## Project layout

| File | Purpose |
|------|---------|
| `main.py` | MCP server: tool definitions and transport selection |
| `openfda_client.py` | openFDA queries and the label-selection logic |
| `label_format.py` | Trims a raw label to the requested sections |
| `faers_format.py` | Decodes and condenses adverse event reports |
| `tests/` | Offline and live tests |

## Roadmap

- `check_recall` tool for recall and enforcement data
- Lookup by NDC or generic name

## Important notes

- **Not medical advice.** This server retrieves public FDA data for information only. It does not replace professional clinical judgment and should not be the sole basis for any treatment decision.
- **Adverse event reports do not prove causation.** FAERS reports are unverified, may be duplicated, and counts reflect reports, not incidence.
- **Data source.** All data comes from openFDA. This project is not affiliated with or endorsed by the FDA.

## About

Built by **J. Paul Doolittle**, retired pharmacist (RPh) and agentic AI engineer, [Data Compounded, LLC](https://datacompounded.com).

I build MCP servers and clinical decision support tools for pharmacy and health-tech teams. Built by a pharmacist, not translated for one.
