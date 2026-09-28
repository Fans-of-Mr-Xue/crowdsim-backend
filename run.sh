#!/usr/bin/env bash

set -euo pipefail

BACKEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONDA_BIN="${CROWDSIM_CONDA:-/opt/miniconda3/bin/conda}"
CONDA_ENV="${CROWDSIM_CONDA_ENV:-sumo}"

if [[ ! -x "${CONDA_BIN}" ]]; then
  echo "[CrowdSim] 未找到 Conda：${CONDA_BIN}" >&2
  echo "请通过 CROWDSIM_CONDA 指定 Conda 可执行文件。" >&2
  exit 1
fi

cd "${BACKEND_ROOT}"
export PYTHONPATH="${BACKEND_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${CONDA_BIN}" run --no-capture-output -n "${CONDA_ENV}" \
  python "${BACKEND_ROOT}/crowdsim_overlay_server.py" \
  --scenario hotspot \
  --host "${CROWDSIM_HOST:-127.0.0.1}" \
  --port "${CROWDSIM_PORT:-8765}" \
  "$@"
