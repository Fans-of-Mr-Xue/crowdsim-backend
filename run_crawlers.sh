#!/usr/bin/env bash
set -euo pipefail
CRAWLER_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "${CRAWLER_ROOT}"
export PYTHONPATH="${CRAWLER_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
if [[ -n "${CROWDSIM_PYTHON:-}" ]]; then
  exec "${CROWDSIM_PYTHON}" -m data_service.crawlers "$@"
elif [[ -n "${CROWDSIM_CONDA:-}" ]]; then
  exec "${CROWDSIM_CONDA}" run --no-capture-output -n "${CROWDSIM_CONDA_ENV:-sumo}" python -m data_service.crawlers "$@"
elif [[ -x "${CRAWLER_ROOT}/.venv-backend/bin/python" ]]; then
  exec "${CRAWLER_ROOT}/.venv-backend/bin/python" -m data_service.crawlers "$@"
else
  exec python -m data_service.crawlers "$@"
fi
