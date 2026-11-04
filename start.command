#!/bin/bash
# Universal Media Extractor - macOS / Linux launcher
# Double-click on macOS, or run:  bash start.command
cd "$(dirname "$0")" || exit 1

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "Python 3.9+ was not found."
  echo "Install it first: https://www.python.org/downloads/"
  read -r -p "Press Enter to close..."
  exit 1
fi

exec "$PY" launcher.py
