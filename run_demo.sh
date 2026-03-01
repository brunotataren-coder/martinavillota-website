#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

export PATH="$HOME/.local/bin:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv no encontrado. Instalando..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d ".venv311" ]; then
  uv venv --python 3.11 .venv311
fi

source .venv311/bin/activate
uv pip install -q cdp-sdk

MODE="${1:-mock}"
if [ "$MODE" = "cdp" ]; then
  if [ ! -f ".env" ]; then
    echo "Falta .env. Crea uno desde .env.example y completa CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET"
    exit 1
  fi
  export WALLET_PROVIDER=cdp
else
  export WALLET_PROVIDER=mock
fi

python server.py
