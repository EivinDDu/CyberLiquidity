#!/bin/bash
# CyberLiquidity launcher for macOS: double-click this file in Finder (or run ./start.command).
# The first start creates .venv and installs the pinned dependencies (needs internet once);
# later starts open the app straight away at http://localhost:8501.
cd "$(dirname "$0")" || exit 1
set -e
if [ ! -x .venv/bin/streamlit ]; then
  PY=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      PY="$candidate"; break
    fi
  done
  if [ -z "$PY" ]; then
    echo "CyberLiquidity needs Python 3.11 or newer (tested with 3.13)."
    read -n 1 -s -r -p "Press any key to close this window."
    exit 1
  fi
  echo "First start: creating .venv with $PY and installing the pinned dependencies..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  .venv/bin/python -m pip install --quiet -e .
fi
echo ""
echo "  CyberLiquidity is starting at  http://localhost:8501"
echo "  Your browser opens automatically. Keep this window open while you use the app;"
echo "  press Ctrl+C here (or close the window) to stop it."
echo ""
exec .venv/bin/streamlit run app/streamlit_app.py
