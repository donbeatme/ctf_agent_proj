#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

export EVALUATOR=smoke
export EVALUATOR_PLAN=mock
export EVALUATOR_STEP=mock
export EVALUATOR_TASK=mock
export CTF_AUDIT_MODE=offline
export RAGFLOW_ENABLED=false

if [ "${1:-}" = "run-task" ]; then
    echo "run-task 固定使用真实 Planner；请配置 LLM_API_KEY 后用 .venv/bin/python main.py run-task。" >&2
    exit 2
fi

exec "$ROOT/.venv/bin/python" "$ROOT/main.py" "$@"
