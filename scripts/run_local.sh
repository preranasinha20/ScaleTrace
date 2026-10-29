#!/usr/bin/env sh
# Set up and start the ScaleTrace web app locally (macOS and Linux).
#
#   sh scripts/run_local.sh              # then open http://127.0.0.1:8000
#   PORT=8010 sh scripts/run_local.sh    # use another port
#   PYTHON=/path/to/python3 sh scripts/run_local.sh
#
# Creates .venv (if missing), installs ScaleTrace with the web API, builds the frontend and starts the server
# on 127.0.0.1 only. Safe to run again: it reuses .venv and reinstalls quickly.
set -eu
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ (or: brew install python)," >&2
  echo "or point PYTHON at it: PYTHON=/path/to/python3 sh scripts/run_local.sh" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "ScaleTrace needs Python 3.10 or newer; found $("$PYTHON" --version 2>&1)." >&2
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "Node.js 18 or newer (with npm) was not found. Install it from https://nodejs.org (or: brew install node)." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creating the virtual environment in .venv ..."
  "$PYTHON" -m venv .venv
fi
if [ -x .venv/bin/python ]; then VENV_PY=.venv/bin/python; else VENV_PY=.venv/Scripts/python.exe; fi

echo "Installing ScaleTrace and the web API ..."
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -e ".[api]"

echo "Building the frontend ..."
npm --prefix frontend ci --no-audit --no-fund
npm --prefix frontend run build

echo ""
echo "ScaleTrace is running at http://127.0.0.1:${PORT}  (press Ctrl+C to stop)"
exec "$VENV_PY" -m scaletrace.api --host 127.0.0.1 --port "$PORT"
