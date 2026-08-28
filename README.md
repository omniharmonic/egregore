# Egregore

**A collective dreaming engine for gathered spaces.**

Egregore listens to the conversations happening across a physical space,
distills them into abstract themes, and renders those themes as continuously
evolving, symbolic video on screens throughout the room. Speech never leaves
the building; only an abstracted, non-attributable prompt ever crosses the
network boundary, and only in cloud mode.

- [PRD](docs/egregore-01-prd.md)
- [Technical Architecture](docs/egregore-02-architecture.md)
- [Implementation Plan](docs/egregore-03-implementation-plan.md)

## Quick start (demo mode — no mic, no GPU, no API key)

```bash
uv sync --extra dev
uv run egregore run presets/demo.yaml
# open http://localhost:8420/?zone=main  (password: printed at startup)
```

Demo mode drives the full real pipeline — ring buffer, theme abstraction,
privacy validator, spend ledger, generation queue, growing loop, WebGL lens —
from a scripted fixture conversation and a procedural local video backend.

## Tests

```bash
uv run pytest
```

`tests/test_privacy.py` is the test that must never fail.
