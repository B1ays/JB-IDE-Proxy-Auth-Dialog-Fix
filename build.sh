#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    "$PYTHON_BIN" -m venv "$PROJECT_DIR/.venv"
fi

"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt" pyinstaller

"$VENV_PYTHON" -m PyInstaller \
    --onefile \
    --console \
    --clean \
    --name JetBrainsProxyPatcher \
    --collect-all textual \
    --collect-all rich \
    "$PROJECT_DIR/jetbrains_proxy.py"

printf '\nBuilt: %s\n' "$PROJECT_DIR/dist/JetBrainsProxyPatcher"
