# Contributing to RouteForge

Keep changes small, deterministic, and auditable.

- Do not add client code, customer data, credentials, private prompts, or screenshots.
- If a constraint is hard, test it in candidate selection and in `evaluate_plan`.
- If a heuristic is an approximation, name it as such in code and docs.
- Preserve seeded instance generation and add a regression test for new behavior.

Run:

```bash
uv run pytest
uv run --with ruff ruff check src tests
uv lock --check
uv build
```
