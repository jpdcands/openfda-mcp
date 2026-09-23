
## Testing

```bash
uv sync                   # installs pytest too (dev group)
uv run pytest             # fast offline tests, no network
uv run pytest -m live     # also hits the real OpenFDA API
```

Run both before every push. The live tests check that common drugs
(metformin, lisinopril, warfarin, ...) return the single-ingredient label
and that every response fits inside Claude's 25,000-token tool-output limit.
