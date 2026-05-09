# CHANGELOG

<!-- version list -->

## v1.15.3 (2026-05-09)

### Bug Fixes

- **deps**: Bump python-multipart 0.0.26 -> 0.0.27 to patch DoS CVE
  ([#71](https://github.com/TETRA-2023/pytaiga-mcp/pull/71),
  [`2c994a4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/2c994a413a25d06eb35c41f80c86fab1da538f95))


## v1.15.2 (2026-05-09)

### Bug Fixes

- List_user_stories silently drops swimlane filter (upstream taiga-back typo)
  ([#69](https://github.com/TETRA-2023/pytaiga-mcp/pull/69),
  [`e5696fb`](https://github.com/TETRA-2023/pytaiga-mcp/commit/e5696fb2657d46eecb3a535c6c2ca27c796c3427))

### Chores

- Add gitleaks to pre-commit ([#65](https://github.com/TETRA-2023/pytaiga-mcp/pull/65),
  [`8aef1ed`](https://github.com/TETRA-2023/pytaiga-mcp/commit/8aef1ed78a2a718113d72f0afbb58043c19b82e2))

### Continuous Integration

- Add gitleaks job and decouple :stable from default-branch builds
  ([#66](https://github.com/TETRA-2023/pytaiga-mcp/pull/66),
  [`b94a6d8`](https://github.com/TETRA-2023/pytaiga-mcp/commit/b94a6d877f6140604456971419a43ec42e1fa5ca))

- Switch default branch references from master to main
  ([#67](https://github.com/TETRA-2023/pytaiga-mcp/pull/67),
  [`9f6f52c`](https://github.com/TETRA-2023/pytaiga-mcp/commit/9f6f52c2e413896f35a8c88dccf3427f33db7fd5))


## v1.15.1 (2026-05-06)

### Bug Fixes

- **deps**: Bump GitPython 3.1.46 -> 3.1.50 to patch CVEs
  ([#64](https://github.com/TETRA-2023/pytaiga-mcp/pull/64),
  [`9833cda`](https://github.com/TETRA-2023/pytaiga-mcp/commit/9833cdab928ae5b19c88e09fca097e0cea0c4dc5))

### Chores

- **inspect.sh**: Replace hardcoded upstream-author path with generic placeholder
  ([`0232251`](https://github.com/TETRA-2023/pytaiga-mcp/commit/0232251dcf2729123780d2f1915170a3ac771077))

### Documentation

- List Swimlanes in README features ([#62](https://github.com/TETRA-2023/pytaiga-mcp/pull/62),
  [`488d853`](https://github.com/TETRA-2023/pytaiga-mcp/commit/488d853dc00f0b0d7d17b3283693a8ce3a55c833))


## v1.15.0 (2026-04-25)

### Features

- Add swimlane CRUD tools and bulk swimlane assignment
  ([#60](https://github.com/TETRA-2023/pytaiga-mcp/pull/60),
  [`7c5444e`](https://github.com/TETRA-2023/pytaiga-mcp/commit/7c5444ef9c97423d8b6ac72d43e3a09f5293b2f0))


## v1.14.6 (2026-04-25)

### Bug Fixes

- Validate milestone_id on bulk_create_tasks with Kanban-aware error
  ([#59](https://github.com/TETRA-2023/pytaiga-mcp/pull/59),
  [`230dbc2`](https://github.com/TETRA-2023/pytaiga-mcp/commit/230dbc2fe9ba8cc38f0e818d259dfd16ffccdf87))


## v1.14.5 (2026-04-25)

### Bug Fixes

- Surface DRF-format Taiga error bodies dropped by pytaigaclient
  ([#58](https://github.com/TETRA-2023/pytaiga-mcp/pull/58),
  [`8623cc9`](https://github.com/TETRA-2023/pytaiga-mcp/commit/8623cc9e0df42c52329726d994b74d285e3c8f5b))


## v1.14.4 (2026-04-17)

### Bug Fixes

- Pass history entry id as query param for comment tools
  ([#54](https://github.com/TETRA-2023/pytaiga-mcp/pull/54),
  [`d9136b3`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d9136b3dcc20d19a9f01118f35d567e1022c5712))


## v1.14.3 (2026-04-17)

### Bug Fixes

- Address review feedback on PR #51
  ([`d9d0865`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d9d086515e00247c4bf43c4576b66d415c05e10e))

- Document kwargs JSON pattern and raise on empty-kwargs updates
  ([`3732dd3`](https://github.com/TETRA-2023/pytaiga-mcp/commit/3732dd30e639013f24ca81e1c9035e0861996c3f))

### Chores

- Sync uv.lock with pyproject v1.14.1
  ([`673f3e1`](https://github.com/TETRA-2023/pytaiga-mcp/commit/673f3e11b5c87def3672b3e2167411fe53c01e37))

### Documentation

- Document ValueError propagation in _execute_taiga_operation Raises
  ([`a006fb4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/a006fb4f52782e3fd02832d6b29ab2a097c22957))


## v1.14.2 (2026-04-14)

### Bug Fixes

- Clarify nested task verbosity behavior in US tool descriptions
  ([`7ff220b`](https://github.com/TETRA-2023/pytaiga-mcp/commit/7ff220beb6e6201393102166259baa0018d0a4bf))

### Chores

- Update dependencies to resolve security alerts
  ([`ff6ff1c`](https://github.com/TETRA-2023/pytaiga-mcp/commit/ff6ff1c89c26f89e5a22021f0b21b13ea588d618))

### Documentation

- Update README to reflect standalone status and full feature set
  ([`5a5e4a6`](https://github.com/TETRA-2023/pytaiga-mcp/commit/5a5e4a65cd9db2ae999112edd7a3acaca8a4e3f4))


## v1.14.1 (2026-04-14)

### Bug Fixes

- Also unescape \t and add trailing slash to comment_versions endpoint
  ([`e8bbe0c`](https://github.com/TETRA-2023/pytaiga-mcp/commit/e8bbe0ca8417b2f9118bb62d18d078853193dfca))

- Comment edit/delete 404 and literal \n in comment text
  ([`d47c1f4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d47c1f40bbaf199d7abbca0de0d111dbc0ba2930))


## v1.14.0 (2026-04-14)

### Bug Fixes

- Eliminate double session resolution and add enrichment tests
  ([`d16a825`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d16a8257858845f916f3d8dfa97e2dea6f4ebf95))

### Features

- Enrich user story responses with associated tasks
  ([`3c9c60a`](https://github.com/TETRA-2023/pytaiga-mcp/commit/3c9c60a21184ab7172d187ca9b2b5731314ae189))


## v1.13.0 (2026-03-26)

### Features

- Add project configuration CRUD tools for statuses, types, priorities, severities
  ([`c8f1398`](https://github.com/TETRA-2023/pytaiga-mcp/commit/c8f139873ddcc850e28bd63d3d86cd4c93c54003))


## v1.12.0 (2026-03-26)

### Bug Fixes

- Remove redundant inline import of os in create_attachment
  ([`7d5b94e`](https://github.com/TETRA-2023/pytaiga-mcp/commit/7d5b94e582fbb6deb44c0be6fc611c3eaa96f9a6))

### Features

- Add attachment tools for all entity types
  ([`9cf9c2f`](https://github.com/TETRA-2023/pytaiga-mcp/commit/9cf9c2fcdbcddbb8651df5ec008919a670db3d60))


## v1.11.0 (2026-03-26)

### Bug Fixes

- Address PR #41 review findings for custom attributes
  ([`a032b96`](https://github.com/TETRA-2023/pytaiga-mcp/commit/a032b96ae3d92d56dcdedb2c36c4058248c32a6a))

### Features

- Add custom attributes tools for entity metadata management
  ([`22b6e8b`](https://github.com/TETRA-2023/pytaiga-mcp/commit/22b6e8bf21ff0a219982e696f924ad65a52b697e))


## v1.10.0 (2026-03-26)

### Bug Fixes

- Address review findings for bulk operations
  ([`5d930b8`](https://github.com/TETRA-2023/pytaiga-mcp/commit/5d930b83952cd7272db6a803c43a4cc6744d230b))

### Features

- Add bulk operations tools for batch entity management
  ([`d48165f`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d48165f7d40a66a51260dd4bef2f4d6c44aca362))


## v1.9.0 (2026-03-26)

### Features

- Add story points management tools
  ([`5c5209b`](https://github.com/TETRA-2023/pytaiga-mcp/commit/5c5209b31ac8198c654a16042e24212f01e72d1c))


## v1.8.0 (2026-03-26)

### Features

- Add full history/audit trail tool
  ([`881212b`](https://github.com/TETRA-2023/pytaiga-mcp/commit/881212b9f665f38f48815d7237c72cf46207da23))


## v1.7.0 (2026-03-26)

### Features

- Add comment edit/delete/undelete and version history tools
  ([`174de29`](https://github.com/TETRA-2023/pytaiga-mcp/commit/174de29d96d8ab4d6ccac709664cabe24b41222b))


## v1.6.0 (2026-03-26)

### Features

- Add project tag management tools
  ([`7d425c1`](https://github.com/TETRA-2023/pytaiga-mcp/commit/7d425c15ee69c4dbe3b046ab98a900fa8bcd3716))


## v1.5.0 (2026-03-26)

### Features

- Add global search tool and CLAUDE.md
  ([`0c9981b`](https://github.com/TETRA-2023/pytaiga-mcp/commit/0c9981bde6bd41bd6b3f7b2f634fee1345a3fa80))


## v1.4.0 (2026-03-24)

### Bug Fixes

- Bind SSE/HTTP server to MCP_HOST for Docker compatibility
  ([`01edeb4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/01edeb4b8371b7b57b6045dda2ac10e50dc6f899))

- Validate MCP_PORT and document MCP_HOST/MCP_PORT env vars
  ([`09c10a2`](https://github.com/TETRA-2023/pytaiga-mcp/commit/09c10a263dd630cfcee4d88b20ba78ea52863eea))

### Features

- Add update, delete, and get-by-slug wiki page tools
  ([`f43e346`](https://github.com/TETRA-2023/pytaiga-mcp/commit/f43e346577a1cf747d68497b16cb8f58c4005e0b))


## v1.3.1 (2026-03-24)

### Bug Fixes

- Bind SSE/HTTP server to MCP_HOST for Docker compatibility
  ([`c80d1b0`](https://github.com/TETRA-2023/pytaiga-mcp/commit/c80d1b0d49bebbec15d679f337ac4e035db12640))


## v1.3.0 (2026-03-24)

### Features

- Add SSE and streamable-http transport support
  ([`ede5586`](https://github.com/TETRA-2023/pytaiga-mcp/commit/ede5586c526b3cfde73e1d8f7c821a6d37b25434))


## v1.2.3 (2026-03-16)

### Bug Fixes

- Disable pagination on all list calls via x-disable-pagination header
  ([`ce35ae4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/ce35ae46fc228633552211c00a6e392d405fa8c1))


## v1.2.2 (2026-03-15)

### Bug Fixes

- **server**: Handle invalid JSON gracefully in _parse_mcp_kwargs
  ([`355e6a8`](https://github.com/TETRA-2023/pytaiga-mcp/commit/355e6a8c8da95c209fc72d5d3e214c929ec6fb05))

- **server**: Set default session on login and use correct slug lookup
  ([`2148586`](https://github.com/TETRA-2023/pytaiga-mcp/commit/2148586bdf60fa93b5ba9342df41d36a42a8c732))

### Documentation

- Update README for Python 3.12, GHCR image, pre-commit hooks
  ([`d28ffc2`](https://github.com/TETRA-2023/pytaiga-mcp/commit/d28ffc2db91e2f4384690025e75370a070382327))

### Testing

- Add filters key path coverage for _parse_mcp_kwargs
  ([`30681e5`](https://github.com/TETRA-2023/pytaiga-mcp/commit/30681e5b0c15c6a8badc95f3619a4df3acb083dc))

- Remove duplicate get_project_by_slug test
  ([`662438c`](https://github.com/TETRA-2023/pytaiga-mcp/commit/662438ce8493eaeded0c7e87d7b122df0268c394))


## v1.2.1 (2026-03-15)

### Bug Fixes

- **ci**: Bump GitHub Actions to Node.js 24 compatible versions
  ([`0c105a4`](https://github.com/TETRA-2023/pytaiga-mcp/commit/0c105a46d42185c7196241d4c66c6f04406d6ce9))


## v1.2.0 (2026-03-15)

### Bug Fixes

- Align requires-python and ruff target to 3.12 matching CI
  ([`70a7e0e`](https://github.com/TETRA-2023/pytaiga-mcp/commit/70a7e0e1be3450f875b89c7547d9d73dd2c56d55))

- **ci**: Add contents:read permission, lowercase GHCR tags, robust version extraction
  ([`1bcd6e7`](https://github.com/TETRA-2023/pytaiga-mcp/commit/1bcd6e7d161d569720b1605c5e5ac9dd958b7ce6))

- **ci**: Align ruff version between pre-commit and CI, standardize Python 3.12
  ([`191edf1`](https://github.com/TETRA-2023/pytaiga-mcp/commit/191edf139e4b8339f09267e49e4c617ab9ece80f))

- **ci**: Validate Docker tag version, decouple release from docker, drop Python matrix
  ([`43a122c`](https://github.com/TETRA-2023/pytaiga-mcp/commit/43a122cdc455f784183f0cd9400483b62d8a7473))

### Features

- **ci**: Add Docker image build and push to GHCR
  ([`29136c6`](https://github.com/TETRA-2023/pytaiga-mcp/commit/29136c6511fc083833ef7e90cbbfc75ada645fc4))

- **dev**: Add pre-commit hooks for ruff lint, format, and unit tests
  ([`4f85e56`](https://github.com/TETRA-2023/pytaiga-mcp/commit/4f85e565953d83feabf6abb18792c9f46cf3dae3))


## v1.0.1 (2026-02-09)

### Bug Fixes

- **server**: Implement .edit() for partial updates and harden integration tests
  ([`73f1171`](https://github.com/talhaorak/pytaiga-mcp/commit/73f1171cb44e7671014a792b4b6033c964711446))

### Code Style

- **verify**: Use dict literals for kwargs in verify_tools.py
  ([`f36f891`](https://github.com/talhaorak/pytaiga-mcp/commit/f36f891da3159ed9589a72c735b7a36eaf72bd47))

### Refactoring

- Address PR review feedback (fix api access, harden updates, clean tests)
  ([`67bb0bd`](https://github.com/talhaorak/pytaiga-mcp/commit/67bb0bd5114a0a2d3cc0e339771432c2d8341c75))

### Testing

- Align unit tests with new .edit() usage in update_project
  ([`31681a3`](https://github.com/talhaorak/pytaiga-mcp/commit/31681a3a5ec68ce71f7ce2a869750a8a1c43296c))


## v1.0.0 (2026-02-06)

- Initial Release
