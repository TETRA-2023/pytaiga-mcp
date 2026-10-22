#!/bin/bash

# Pass-through: ./run.sh [--sse | --streamable-http]
exec uv run python src/server_workflow.py "$@"
