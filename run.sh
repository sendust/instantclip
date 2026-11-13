#!/usr/bin/env bash
# instantclip launcher: creates the venv on first run, then starts the backend.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m app.main "$@"
