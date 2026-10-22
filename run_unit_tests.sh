#!/bin/bash

# Run every unit test (everything not marked integration) — matches CI and the pre-commit hook
uv run pytest -m "not integration" -v
