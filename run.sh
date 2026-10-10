#!/usr/bin/env bash

set -euo pipefail

BACKEND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${CROWDSIM_PYTHON:-}"

if [[ -z "${PYTHON_BIN}" ]]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    PYTHON_BIN="python3"
  fi
fi

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
  echo "[CrowdSim] 当前终端未找到 Python：${PYTHON_BIN}" >&2
  echo "请准备好 Python 环境，或通过 CROWDSIM_PYTHON 指定解释器。" >&2
  exit 1
fi

cd "${BACKEND_ROOT}"
export PYTHONPATH="${BACKEND_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

exec "${PYTHON_BIN}" "${BACKEND_ROOT}/crowdsim_overlay_server.py" \
  --scenario hotspot \
  --host "${CROWDSIM_HOST:-127.0.0.1}" \
  --port "${CROWDSIM_PORT:-8765}" \
  "$@"
