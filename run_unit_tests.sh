#!/bin/bash

# Run unit tests (full + workflow servers + client shim + transport) — matches CI and the pre-commit hook
uv run pytest tests/test_server.py tests/test_server_workflow.py tests/test_taiga_client.py tests/test_transport.py -v
