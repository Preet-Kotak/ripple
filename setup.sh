#!/usr/bin/env bash
# One-command setup for Mac/Linux:  bash setup.sh
set -e
cd "$(dirname "$0")"
PY=python3; command -v python3 >/dev/null || PY=python
[ -d venv ] || $PY -m venv venv
source venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Created .env. Open it, paste your Atlas connection string, save, and run: bash setup.sh"
  exit 1
fi
python scripts/smoke_test.py
echo ""
echo "All good. Start the web page with:  source venv/bin/activate && streamlit run app.py"
