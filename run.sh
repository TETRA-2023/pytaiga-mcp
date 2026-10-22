#!/bin/bash

# Pass-through: ./run.sh [--sse | --streamable-http]
exec uv run mcp-taiga "$@"
