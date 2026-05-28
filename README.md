<p align="center">
<picture>
  <img src="https://taiga.io/media/images/favicon.width-44.png">
</picture>
</p>

# Taiga MCP Bridge


[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![GHCR](https://img.shields.io/badge/ghcr.io-tetra--2023%2Fpytaiga--mcp-blue?logo=docker)](https://ghcr.io/tetra-2023/pytaiga-mcp)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

> Originally forked from [talhaorak/pytaiga-mcp](https://github.com/talhaorak/pytaiga-mcp), now an independent standalone project. The codebase has diverged significantly with 100+ tools, Docker/CI support, multi-transport modes, and active maintenance.

## Overview

The Taiga MCP Bridge is a powerful integration layer that connects [Taiga](https://taiga.io/) project management platform with the Model Context Protocol (MCP), enabling AI tools and workflows to interact seamlessly with Taiga's resources.

This bridge provides a comprehensive set of tools and resources for AI agents to:
- Create and manage projects, epics, user stories, tasks, and issues in Taiga
- Track sprints and milestones
- Assign and update work items
- Query detailed information about project artifacts
- Manage project members and permissions

By using the MCP standard, this bridge allows AI systems to maintain contextual awareness about project state and perform complex project management tasks programmatically.

## Two server modes

Starting with v2.0, this bridge ships two servers in a single image. Select one at runtime via the `TAIGA_SERVER_MODE` environment variable:

| Mode | Tools | Best for |
|---|---|---|
| `workflow` (default) | ~27 intent tools | Product Owners and team members managing projects daily |
| `full` | 107 CRUD tools | Automation scripts, admin tasks, full API access |

**Workflow mode** (default) accepts human-readable names everywhere — project slug, sprint name, status name, username — and resolves them internally. One call to `get_sprint_board` returns a complete board view with all stories and tasks; `plan_sprint` creates a sprint and assigns stories in a single step.

**Full mode** gives direct access to every Taiga API endpoint. Useful when you need precise control, bulk operations, or access to resources not surfaced in workflow mode (swimlanes, custom attributes, attachments, story points).

Switch mode at runtime:

```bash
# Docker: set env var
docker run -e TAIGA_SERVER_MODE=full ...

# Local: env var or .env file
TAIGA_SERVER_MODE=full ./run.sh
```

## Features

### Workflow mode tools (~27)

Intent-based tools that resolve names and composite API calls:

| Tool | What it does |
|---|---|
| `get_project_overview` | Project snapshot: team, active sprint, story counts by status |
| `browse_backlog` | Filtered backlog view with name-based filters (status, assignee, epic) |
| `create_story` | Create US with optional epic link, sprint, assignee — one call |
| `update_story` | Update any US field by name (status, assignee, sprint, epic) |
| `create_task` | Add a single task under a user story |
| `update_task` | Update a task by ref (status, assignee, sprint, reparent to another story) |
| `set_task_status` | Change task status by name |
| `break_down_story` | Decompose a story into multiple tasks in one call (bulk-aware) |
| `create_issue` | Create issue with smart defaults for type/priority/severity |
| `update_issue` | Update any issue field by name |
| `get_sprint_board` | Full sprint view: all stories + tasks, summary by status |
| `plan_sprint` | Create sprint + assign stories in one step |
| `move_to_sprint` | Move stories by ref to a named sprint |
| `set_story_status` | Change story status by name (e.g. "Done") |
| `close_sprint` | Mark a sprint closed |
| `get_epic_overview` | Epic + all linked stories with progress counts |
| `create_epic` | Create epic + optionally link existing stories |
| `get_team_workload` | Per-member story/task counts for a sprint |
| `assign_item` | Assign any entity by username |
| `get_wiki` / `upsert_wiki` | Get or create/update wiki pages |
| `add_comment` | Comment on any entity by ref |
| `search` | Full-text search across all entity types |

### Full mode tools (107+)

The bridge provides comprehensive CRUD operations and advanced features across all Taiga resources:

- **Projects**: Create, update, manage settings, tags, and project configuration (statuses, types, priorities, severities)
- **Epics**: Manage large features, link user stories to epics
- **User Stories**: Full lifecycle management with automatic task enrichment in responses
- **Tasks**: Track work within user stories, bulk create and reorder
- **Issues**: Manage bugs, questions, and enhancement requests
- **Sprints (Milestones)**: Plan and track work in time-boxed intervals
- **Swimlanes**: Manage Kanban swimlanes (CRUD), assign user stories individually or in bulk per status
- **Wiki Pages**: Create, update, delete, and look up by slug
- **Comments**: Add, edit, delete, undelete, and view version history on any object
- **Attachments**: Upload and manage attachments on all entity types
- **Custom Attributes**: Define and set custom metadata fields per entity type
- **Bulk Operations**: Batch create epics, user stories, tasks, issues, and memberships; bulk-update user story milestone, kanban order, and swimlane assignment
- **Story Points**: Manage point scales and assignments
- **History/Audit Trail**: View change history for any object
- **Global Search**: Search across all project resources
- **Memberships**: Manage project members and invite users

### Security & Configuration

- **Secure Credentials**: Environment variable authentication with credential protection - passwords never appear in logs or error messages
- **Auto-Authentication**: Configure `TAIGA_USERNAME` and `TAIGA_PASSWORD` environment variables for seamless startup without manual login
- **Input Validation**: Allowlist-based parameter validation prevents unexpected data from reaching the Taiga API

### Response Filtering

All tools support a `verbosity` parameter to control response size, reducing AI context usage:

| Level | Description | Use Case |
|-------|-------------|----------|
| `minimal` | Core fields only (id, ref, subject, status, project) | Listing many items |
| `standard` | Common fields including version for updates (default) | Normal operations |
| `full` | Complete API response | Debugging, full details |

Example:
```python
# Get minimal response for efficient context usage
stories = client.call_tool("list_user_stories", {
    "project_id": 123,
    "verbosity": "minimal"
})
# Returns: [{"id": 1, "ref": 42, "subject": "...", "status": 1, "project": 123}, ...]
```

## Installation

This project uses [uv](https://github.com/astral-sh/uv) for fast, reliable Python package management.

### Prerequisites

- Python 3.12 or higher
- uv package manager

### Basic Installation

```bash
# Clone the repository
git clone https://github.com/TETRA-2023/pytaiga-mcp.git
cd pytaiga-mcp

# Install dependencies
./install.sh
```

### Development Installation

For development (includes testing and code quality tools):

```bash
./install.sh --dev
```

### Manual Installation

If you prefer to install manually:

```bash
# Production dependencies only
uv pip install -e .

# With development dependencies
uv pip install -e ".[dev]"
```

### Docker

The image ships both servers. Mode is selected by the `TAIGA_SERVER_MODE` env var; default is `workflow`.

Pull and run (workflow mode, stdio):

```bash
docker run -i --rm \
  -e TAIGA_API_URL=https://your-taiga-instance.com \
  -e TAIGA_USERNAME=your_username \
  -e TAIGA_PASSWORD=your_password \
  ghcr.io/tetra-2023/pytaiga-mcp:latest
```

Full mode (set `TAIGA_SERVER_MODE=full`):

```bash
docker run -i --rm \
  -e TAIGA_API_URL=https://your-taiga-instance.com \
  -e TAIGA_USERNAME=your_username \
  -e TAIGA_PASSWORD=your_password \
  -e TAIGA_SERVER_MODE=full \
  ghcr.io/tetra-2023/pytaiga-mcp:latest
```

SSE transport (append `--sse`, expose a port):

```bash
docker run --rm \
  -e TAIGA_API_URL=https://your-taiga-instance.com \
  -e TAIGA_USERNAME=your_username \
  -e TAIGA_PASSWORD=your_password \
  -p 8000:8000 \
  ghcr.io/tetra-2023/pytaiga-mcp:latest --sse
```

Or build locally:

```bash
docker build -t pytaiga-mcp .
# Run full mode from local build:
docker run -i --rm -e TAIGA_SERVER_MODE=full -e TAIGA_API_URL=... pytaiga-mcp
```

> **Note**: `TAIGA_SERVER_MODE` is read at container startup. To switch modes, restart the container with a different value — there is no need to pull a different image.

Example MCP client configuration (`.mcp.json`) for stdio transport:

```json
{
  "mcpServers": {
    "taiga": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "TAIGA_API_URL",
        "-e", "TAIGA_USERNAME",
        "-e", "TAIGA_PASSWORD",
        "ghcr.io/tetra-2023/pytaiga-mcp:latest"
      ]
    }
  }
}
```

> **Note**: Use `-i` (interactive) without `-t` (pseudo-TTY) for stdio transport. The `-e VAR` form (without `=value`) forwards the variable from your host environment.

## Configuration

The bridge can be configured through environment variables or a `.env` file:

| Environment Variable | Description | Default |
| --- | --- | --- |
| `TAIGA_API_URL` | Base URL for the Taiga API | http://localhost:9000 |
| `TAIGA_USERNAME` | Taiga username for auto-authentication | (none) |
| `TAIGA_PASSWORD` | Taiga password for auto-authentication | (none) |
| `TAIGA_TRANSPORT` | Transport mode (stdio, sse, or streamable-http) | stdio |
| `MCP_HOST` | Bind address for SSE/HTTP transport. Use `0.0.0.0` for Docker. | 127.0.0.1 |
| `MCP_PORT` | Listen port for SSE/HTTP transport | 8000 |
| `LOG_LEVEL` | Logging level | INFO |

Create a `.env` file in the project root to set these values:

```
TAIGA_API_URL=https://api.taiga.io/api/v1/
TAIGA_USERNAME=your_username
TAIGA_PASSWORD=your_password
TAIGA_TRANSPORT=stdio
LOG_LEVEL=INFO
```

**Security Note**: Credentials are protected and will never appear in logs, error messages, or stack traces. When `TAIGA_USERNAME` and `TAIGA_PASSWORD` are configured, the server auto-authenticates on startup - no manual login required.

## Usage

### With stdio mode

Paste the following json in your Claude App's or Cursor's mcp settings section.

**Recommended**: Set credentials via environment variables in your shell profile rather than in config files to avoid exposing them in plaintext.

```json
{
    "mcpServers": {
        "taigaApi": {
            "command": "uv",
            "args": [
                "--directory",
                "<path to local pyTaigaMCP folder>",
                "run",
                "src/server.py"
            ],
            "env": {
                "TAIGA_TRANSPORT": "<stdio|sse>",                
                "TAIGA_API_URL": "<Taiga API Url (ex: http://localhost:9000)",
                "TAIGA_USERNAME": "<taiga username>",
                "TAIGA_PASSWORD": "<taiga password>"
            }
        }
}
```

### Running the Bridge

Start the MCP server with:

```bash
# Default stdio transport
./run.sh

# For SSE transport
./run.sh --sse
```

Or manually:

```bash
# For stdio transport (default)
uv run python src/server.py

# For SSE transport
uv run python src/server.py --sse
```

### Transport Modes

The server supports three transport modes:

1. **stdio (Standard Input/Output)** - Default mode for terminal-based clients (Claude Code, Cursor)
2. **SSE (Server-Sent Events)** - Web-based transport with server push capabilities
3. **Streamable HTTP** - HTTP-based transport for stateless deployments

You can set the transport mode in several ways:
- Using `--sse` or `--streamable-http` flags with run.sh or server.py (default is stdio)
- Setting the `TAIGA_TRANSPORT` environment variable
- Adding `TAIGA_TRANSPORT=sse` or `TAIGA_TRANSPORT=streamable-http` to your `.env` file

### Authentication Flow

#### Auto-Authentication (Recommended)

If `TAIGA_USERNAME` and `TAIGA_PASSWORD` environment variables are set, the server automatically authenticates on startup. You can omit `session_id` from tool calls to use the default session:

```python
# No login needed - uses auto-authenticated default session
projects = client.call_tool("list_projects", {})
stories = client.call_tool("list_user_stories", {"project_id": 123})
new_story = client.call_tool("create_user_story", {
    "project_id": 123,
    "subject": "New feature request"
})
```

#### Manual Session Management

For scenarios requiring multiple sessions or explicit control, use the session-based model:

1. **Login**: Authenticate using the `login` tool:
   ```python
   session = client.call_tool("login", {
       "username": "your_taiga_username",
       "password": "your_taiga_password",
       "host": "https://api.taiga.io" # Optional
   })
   # Save the session_id from the response
   session_id = session["session_id"]
   ```

2. **Using Tools and Resources**: Include the `session_id` in every API call:
   ```python
   # For resources, include session_id in the URI
   projects = client.get_resource(f"taiga://projects?session_id={session_id}")
   
   # For project-specific resources
   epics = client.get_resource(f"taiga://projects/123/epics?session_id={session_id}")
   
   # For tools, include session_id as a parameter
   new_project = client.call_tool("create_project", {
       "session_id": session_id,
       "name": "New Project",
       "description": "Description"
   })
   ```

3. **Check Session Status**: You can check if your session is still valid:
   ```python
   status = client.call_tool("session_status", {"session_id": session_id})
   # Returns information about session validity and remaining time
   ```

4. **Logout**: When finished, you can logout to terminate the session:
   ```python
   client.call_tool("logout", {"session_id": session_id})
   ```

### Example: Complete Project Creation Workflow

Here's a complete example of creating a project with epics and user stories:

```python
from mcp.client import Client

# Initialize MCP client
client = Client()

# Authenticate and get session ID
auth_result = client.call_tool("login", {
    "username": "admin",
    "password": "password123",
    "host": "https://taiga.mycompany.com"
})
session_id = auth_result["session_id"]

# Create a new project
project = client.call_tool("create_project", {
    "session_id": session_id,
    "name": "My New Project",
    "description": "A test project created via MCP"
})
project_id = project["id"]

# Create an epic
epic = client.call_tool("create_epic", {
    "session_id": session_id,
    "project_id": project_id,
    "subject": "User Authentication",
    "description": "Implement user authentication features"
})
epic_id = epic["id"]

# Create a user story in the epic
story = client.call_tool("create_user_story", {
    "session_id": session_id,
    "project_id": project_id,
    "subject": "User Login",
    "description": "As a user, I want to log in with my credentials",
    "epic_id": epic_id
})

# Logout when done
client.call_tool("logout", {"session_id": session_id})
```

## Development

### Project Structure

```
pytaiga-mcp/
├── src/
│   ├── server.py          # MCP server implementation with tools
│   ├── taiga_client.py    # Taiga API client wrapper
│   └── config.py          # Configuration settings with Pydantic
├── tests/
│   ├── test_server.py     # Unit tests
│   └── test_integration.py # Integration tests
├── .github/workflows/
│   └── ci.yml             # CI pipeline (test, lint, Docker, release)
├── .pre-commit-config.yaml # Pre-commit hooks (ruff, pytest)
├── Dockerfile             # Container image definition
├── pyproject.toml         # Project configuration and dependencies
├── install.sh             # Installation script
├── run.sh                 # Server execution script
└── README.md              # Project documentation
```

### Testing

Pre-commit hooks run automatically on each commit (ruff lint, ruff format, unit tests). To run manually:

```bash
# Run pre-commit hooks on all files
uv run pre-commit run --all-files

# Run tests directly
uv run pytest tests/test_server.py -v --tb=short

# Run with coverage reporting
uv run pytest --cov=src
```

### Debugging and Inspection

Use the included inspector tool for debugging:

```bash
# Default stdio transport
./inspect.sh

# For SSE transport
./inspect.sh --sse

# For development mode
./inspect.sh --dev
```

## Error Handling

All API operations return standardized error responses in the following format:

```json
{
  "status": "error",
  "error_type": "ExceptionClassName",
  "message": "Detailed error message"
}
```

## Planned Features

The following features are planned for future releases:

- Session expiration and automatic cleanup
- Rate limiting for API calls
- Retry mechanism with exponential backoff
- Connection pooling

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Install development dependencies (`./install.sh --dev`)
4. Set up pre-commit hooks (`uv run pre-commit install`)
5. Make your changes
6. Commit your changes — pre-commit hooks will run linting and tests automatically
7. Push to the branch (`git push origin feature/amazing-feature`)
8. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- [talhaorak](https://github.com/talhaorak) for the [original pytaiga-mcp](https://github.com/talhaorak/pytaiga-mcp) project that this work builds upon
- [Taiga](https://www.taiga.io/) for their excellent project management platform
- [Model Context Protocol (MCP)](https://github.com/mcp-foundation/specification) for the standardized AI communication framework
- All contributors who have helped shape this project
