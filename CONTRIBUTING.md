# Contributing

Pull requests target **`TETRA-2023/pytaiga-mcp`** (this is a maintained fork; changes for the
upstream `talhaorak/pytaiga-mcp` are coordinated separately by the maintainers). Merged
branches are kept, for cherry-picking.

1. Branch from `main`: `feature/<name>` or `fix/<name>`.
2. `./install.sh --dev` and `uv run pre-commit install`.
3. Make the change, with tests (see below).
4. `./run_unit_tests.sh` and `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/`.
5. Commit with [Conventional Commits](https://www.conventionalcommits.org/) — `feat:`, `fix:`,
   `docs:`, `test:`, `ci:`, `chore:`, … — python-semantic-release derives the version from them
   (`fix:` → patch, `feat:` → minor, `!` / `BREAKING CHANGE:` → major). PRs are squash-merged, so
   the **PR title** is the commit that counts.
6. Open the PR. CI runs gitleaks, lint, the unit tests and a container smoke test
   (`initialize` + `tools/list` on the built image).

## Adding or changing a tool

- A tool group is a module in `src/server/tools/`, imported in `src/server/__init__.py`.
  Register tools with `@app.tool(...)` (it runs them off the event loop), and call shared helpers
  through their module — `core._get_authenticated_client(...)`, `resolve._resolve_project(...)` —
  so a single `monkeypatch.setattr(core, ...)` reaches every caller.
- Take the project as slug or ID, items by **ref**, and statuses, sprints, people and roles by
  **name**. Multi-type tools take an `entity_type` argument.
- **Check the endpoint against taiga-back before writing it** (validator field names, side
  effects). Several pre-v3 tools sent field names Taiga never read. Then confirm the behaviour on a
  real Taiga — e.g. `/<segment>/by_ref` answers an unknown ref with a 404, never an empty body
  (use `resolve._get_by_ref`); Taiga silently ignores unknown query parameters and body fields, so
  validate them per tool.
- The tool description is all the calling model sees: state permanent effects, cascades and
  required rights there.

## Tests

- **Unit tests** (`tests/test_server.py`) mock HTTP by path: `route(mock_client, get=/post=/patch=
  {path: value})` answers `client.api.<verb>` and `sent(mock_client, verb, path)` returns the JSON
  sent. Assert the endpoint and payload. Model Taiga's real responses (a 404 is
  `_api_error(TaigaNotFoundError, 404)`, not `None`).
- **The HTTP client** (`tests/test_taiga_client.py`) is tested on `httpx.MockTransport`.
- **Integration tests** (`tests/test_integration.py`, `-m integration`) run against a real Taiga.
  Write tests need `TAIGA_TEST_PROJECT` set to a **throwaway** project; they must delete
  everything they create (including project-level leftovers such as tags) and leave the
  project as they found it.
