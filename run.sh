#!/usr/bin/env bash

set -euo pipefail

# Default: reuse healthy services. After editing backend code, explicitly use
# bash run.sh --restart-services to reload this checkout's local services only.
# Arguments are forwarded unchanged; remote model/database services aren't restarted.
BACKEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${BACKEND_ROOT}"
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
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
  export PYTHONPATH="$(cygpath -m "${BACKEND_ROOT}")${PYTHONPATH:+;${PYTHONPATH}}"
  export PYTHONUTF8="${PYTHONUTF8:-1}"
else
  export PYTHONPATH="${BACKEND_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
fi

if [[ -n "${CROWDSIM_PYTHON:-}" ]]; then
  exec "${CROWDSIM_PYTHON}" "${BACKEND_ROOT}/start_backend.py" "$@"
elif [[ -n "${CROWDSIM_CONDA:-}" ]]; then
  exec "${CROWDSIM_CONDA}" run --no-capture-output -n "${CROWDSIM_CONDA_ENV:-sumo}" \
    python "${BACKEND_ROOT}/start_backend.py" "$@"
elif [[ -x "${BACKEND_ROOT}/.venv-backend/bin/python" ]]; then
  exec "${BACKEND_ROOT}/.venv-backend/bin/python" "${BACKEND_ROOT}/start_backend.py" "$@"
elif [[ -x "${BACKEND_ROOT}/.venv-backend/Scripts/python.exe" ]]; then
  exec "${BACKEND_ROOT}/.venv-backend/Scripts/python.exe" "${BACKEND_ROOT}/start_backend.py" "$@"
else
  exec python "${BACKEND_ROOT}/start_backend.py" "$@"
fi
