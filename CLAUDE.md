# CLAUDE.md — pytaiga-mcp

## Project Overview

MCP (Model Context Protocol) server bridging AI assistants with the Taiga project management API. Built on FastMCP, talking to Taiga's REST API directly through a small httpx client (no SDK).

- **Package**: `mcp-taiga-bridge`
- **Python**: >= 3.12
- **v3 in progress** on the integration branch `feature/taiga-v3` (plan: Taiga tetra #2621). Branch PRs target `feature/taiga-v3`, never `main`; `main` stays the released v2.x until the cut-over.

## One server

Since v3 there is a single server, `src/server_workflow.py` (entry point `mcp-taiga`; `mcp-taiga-workflow` is a one-release alias). The former 107-tool `full` mode and `TAIGA_SERVER_MODE` are gone: the server ignores the variable and logs a warning if it is set to anything but `workflow`. Capabilities kept from full mode come back as intent tools in v3 B5–B9, ported from `git show main:src/server_full.py`.

## Architecture

```
src/
  server_workflow.py — the MCP server: intent tools + session/name-resolution helpers
  taiga_client.py    — TaigaHTTP (httpx), Taiga errors, TaigaClientWrapper, user resolver
  config.py          — Pydantic settings (env vars, SecretStr credentials)
```

- `server_workflow.py` — intent-oriented tools that accept human-readable names (project slug, sprint name, status name, member email or full name) and resolve them internally. Note: Taiga `/memberships` exposes no username field, so user resolution matches on `user_email` / `email` / `full_name` only. Tools composite multiple API calls to answer PO-level questions in one step.
- `taiga_client.py` — `TaigaHTTP` is the whole client: `get/post/patch/put/delete(path, params=, json=, files=, headers=)` against `/api/v1`. Tools call endpoints directly (`client.api.get("/userstories/by_ref", params={"ref": r, "project": pid})`); there are no per-resource classes. `TaigaClientWrapper.list_resources()` lists with `x-disable-pagination`.
- Session management: `active_sessions` dict keyed by UUID (or `"default"` for auto-auth).

## Development Setup

```bash
./install.sh --dev    # Install with dev dependencies (ruff, pytest, mypy, pre-commit)
cp .env.example .env  # Configure TAIGA_API_URL, TAIGA_USERNAME, TAIGA_PASSWORD
```

## Running

```bash
./run.sh                                    # stdio (default)
./run.sh --sse | --streamable-http          # other transports
# Or directly:
uv run python src/server_workflow.py [--sse | --streamable-http]
```

Environment variables: `TAIGA_TRANSPORT`, `TAIGA_API_URL`, `TAIGA_USERNAME`, `TAIGA_PASSWORD`, `MCP_HOST`, `MCP_PORT`.

## Testing

```bash
./run_unit_tests.sh                                        # Every unit test (pytest -m "not integration")
uv run pytest tests/test_integration.py -v -m integration  # Integration tests against a real Taiga
```

- **Unit tests mock HTTP by path.** In `tests/test_server_workflow.py`, `route(mock_client, get=/post=/patch={path: value})` answers `client.api.<verb>` by exact path, and `sent(mock_client, verb, path)` returns the JSON payloads sent. Assert the endpoint and payload, not a mock attribute: a routed write verb fails on any unrouted path. `TaigaHTTP` itself is tested on `httpx.MockTransport` (`tests/test_taiga_client.py`).
- **Integration tests**: read tests run on any account; write tests need `TAIGA_TEST_PROJECT=smartagile` (the sandbox Taiga project, ID 13) and delete what they create. Load credentials literally (as `docker --env-file` does): sourcing `~/.claude/taiga.env` in bash mangles the password and Taiga answers 401.

Pre-commit hooks run gitleaks, ruff (lint + format) and the unit tests automatically.

## Code Conventions

- **Linter/formatter**: ruff (line-length=100, target py312, rules: E, F, W, I)
- **Commit messages**: Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, etc.) — used by `python-semantic-release` for auto-versioning
- **Branch naming**: `feature/<name>`, `fix/<name>`
- **Tool pattern**: intent-based, name-resolution first:
  - All tools accept project slug or ID; status, sprint, assignee accepted by name
  - Name resolution uses `_resolve_project`, `_resolve_sprint`, `_resolve_status`, `_resolve_user` helpers
  - Resolution results are cached per-session in `_session_cache` (cleared on logout)
  - Composite reads (e.g. `get_sprint_board`) batch sub-queries for efficiency
  - Multi-type tools take an `entity_type` argument (see `assign_item`, `add_comment`) rather than one tool per type
- **Error handling**: All tools use `_execute_taiga_operation()` wrapper for consistent error handling
- **Security**: Never log credentials, request bodies or headers. Use `SecretStr` for passwords.

## Key Design Decisions

- Pagination disabled for lists via the `x-disable-pagination: True` header (Taiga default PAGE_SIZE=30 is too low)
- Auth: `POST /auth` returns an access token (24 h) and a refresh token (8 days). On a 401, `TaigaHTTP` refreshes once (`POST /auth/refresh`) and retries. Refresh tokens ROTATE (a spent one is rejected), so refresh runs under a lock and a waiter re-checks the token. After the refresh token expires the session needs a new login (stateless API-token auth: Taiga tetra issue #687).
- Errors: `TaigaAPIError` (and `TaigaBadRequestError`, `TaigaNotFoundError`, … by status) keep the decoded body; `error_detail` is `_error_message`, DRF `detail`, or the joined field errors of a 400.
- Query params are encoded like `requests` did: `None` dropped, booleans as `"True"`/`"False"`.
- Comments use raw HTTP (PATCH with version for optimistic concurrency, GET history for listing)
- Epic links (`create_epic(story_refs=)`, `create_story(epic=)`, `update_story(epic=)`) use one raw POST per story to `/epics/{id}/related_userstories` with `{epic, user_story}`. Never `/related_userstories/bulk_create`: it creates NEW stories from newline-separated subjects (#143)
- `_COMMENT_PATH_MAP` translates user-facing entity types to API path segments

## Contributing

Per CONTRIBUTING.md:
1. Create feature branch (`feature/<name>`)
2. Install dev dependencies (`./install.sh --dev`)
3. Make changes following the code conventions above
4. Run tests (`uv run pytest tests/test_server.py -v`)
5. Ensure ruff passes (`uv run ruff check src/ && uv run ruff format --check src/`)
6. Commit with conventional commit messages
7. Open a Pull Request

**Repository policy**: This is a maintained fork. All PRs must target `TETRA-2023/pytaiga-mcp`. Do not open PRs against the upstream repo (`talhaorak/pytaiga-mcp`) — contributions to upstream should be coordinated separately and manually by maintainers.

**Branch retention**: Do not delete branches after merging. Merged branches are kept to facilitate cherry-picking and PR transfers to upstream.

The project follows the Contributor Covenant Code of Conduct v2.0.
