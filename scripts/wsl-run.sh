#!/bin/bash
# Run a command inside the repo on Linux with the project venv.
#   scripts/wsl-run.sh [py310] python -m pytest -q tests/
# From Windows Git Bash:
#   MSYS_NO_PATHCONV=1 wsl.exe -d Ubuntu-22.04 -- bash /mnt/d/Projects/Gits/ALIVE/scripts/wsl-run.sh python -m pytest -q
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=${ALIVE_VENV:-$HOME/.venvs/alive}
if [ "$1" = "py310" ]; then VENV=$HOME/.venvs/alive310; shift; fi
export PATH=$VENV/bin:$HOME/.local/bin:$PATH
export PYTHONPYCACHEPREFIX=$HOME/.cache/alive-pycache
export PYTHONPATH=$REPO
cd "$REPO"
exec "$@"
