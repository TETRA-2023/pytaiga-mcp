#!/bin/bash

MODE=${TAIGA_SERVER_MODE:-workflow}

if [[ "$1" == "--sse" ]]; then
    uv run python src/server_${MODE}.py --sse
elif [[ "$1" == "--streamable-http" ]]; then
    uv run python src/server_${MODE}.py --streamable-http
else
    uv run python src/server_${MODE}.py
fi
