#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Install dependencies and Playwright browsers, then run the server
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
# Install browsers required by Playwright (with deps where available)
python -m playwright install --with-deps || python -m playwright install

# Defaults
: ${HOST:=127.0.0.1}
: ${PORT:=8000}
: ${RELOAD:=true}

export HOST PORT RELOAD
echo "Starting server on ${HOST}:${PORT} (reload=${RELOAD})"
python server.py
