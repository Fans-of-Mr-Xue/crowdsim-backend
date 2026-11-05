#!/usr/bin/env bash
set -euo pipefail
BACKEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$BACKEND_ROOT"
# Read only the interpreter setting as literal data; never source credentials.
# Explicit environment overrides still take priority over this local setting.
if [[ -z "${CROWDSIM_PYTHON:-}" && -z "${CROWDSIM_CONDA:-}" && -f .env ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*CROWDSIM_PYTHON[[:space:]]*=(.*)$ ]]; then
      value="${BASH_REMATCH[1]}"
      value="${value#"${value%%[![:space:]]*}"}"
      value="${value%"${value##*[![:space:]]}"}"
      if [[ "$value" == \"*\" || "$value" == \'*\' ]]; then value="${value:1:${#value}-2}"; fi
      export CROWDSIM_PYTHON="$value"
      break
    fi
  done < .env
fi
export PYTHONPATH="${BACKEND_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
if [[ -n "${CROWDSIM_PYTHON:-}" ]]; then
  exec "$CROWDSIM_PYTHON" "$BACKEND_ROOT/start_during.py" "$@"
elif [[ -n "${CROWDSIM_CONDA:-}" ]]; then
  exec "$CROWDSIM_CONDA" run --no-capture-output -n "${CROWDSIM_CONDA_ENV:-sumo}" python "$BACKEND_ROOT/start_during.py" "$@"
elif [[ -x "$BACKEND_ROOT/.venv-backend/bin/python" ]]; then
  exec "$BACKEND_ROOT/.venv-backend/bin/python" "$BACKEND_ROOT/start_during.py" "$@"
else
  exec python "$BACKEND_ROOT/start_during.py" "$@"
fi
