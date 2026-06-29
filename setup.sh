#!/usr/bin/env bash
set -euo pipefail

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo "Installing Playwright browsers..."
python -m playwright install --with-deps || python -m playwright install
echo "Setup complete."
