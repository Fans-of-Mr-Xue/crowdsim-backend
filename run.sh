#!/usr/bin/env bash

set -euo pipefail

BACKEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${BACKEND_ROOT}"
export PYTHONPATH="${BACKEND_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

if [[ -n "${CROWDSIM_PYTHON:-}" ]]; then
  exec "${CROWDSIM_PYTHON}" "${BACKEND_ROOT}/start_backend.py" "$@"
elif [[ -n "${CROWDSIM_CONDA:-}" ]]; then
  exec "${CROWDSIM_CONDA}" run --no-capture-output -n "${CROWDSIM_CONDA_ENV:-sumo}" \
    python "${BACKEND_ROOT}/start_backend.py" "$@"
elif [[ -x "${BACKEND_ROOT}/.venv-backend/bin/python" ]]; then
  exec "${BACKEND_ROOT}/.venv-backend/bin/python" "${BACKEND_ROOT}/start_backend.py" "$@"
else
  exec python "${BACKEND_ROOT}/start_backend.py" "$@"
fi
