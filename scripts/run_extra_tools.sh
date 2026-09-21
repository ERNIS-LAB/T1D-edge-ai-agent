#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# Extra with-tools runs for models added later (gemma-4-31b, qwen3.6-27b).
# Same config as run_all_benchmarks_v2.sh: 32k context, DeepSeek judge/sim, temp 0,
# results into benchmark_results_v2/<identifier>.
set -u

CONTEXT_LENGTH=32768
PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
OUTPUT_ROOT="benchmark_results_v2"

MODELS=(
  "google/gemma-4-31b-qat|gemma-4-31b-qat"
  "qwen/qwen3.6-27b|qwen3.6-27b"
)

ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

for entry in "${MODELS[@]}"; do
  load_key="${entry%%|*}"
  id="${entry##*|}"
  echo "=============================================="
  echo "[run_extra_tools] Agent (local, tools): $id  [$load_key]"
  echo "=============================================="

  echo "[run_extra_tools] Loading $load_key as '$id' (context $CONTEXT_LENGTH)..."
  if ! lms load "$load_key" --identifier "$id" -c "$CONTEXT_LENGTH" -y; then
    echo "[run_extra_tools] ERROR: failed to load $load_key, skipping."
    continue
  fi

  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$OUTPUT_ROOT/$id" \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[run_extra_tools] ERROR: benchmark failed for $id."
  fi

  echo "[run_extra_tools] Unloading $id..."
  lms unload --all 2>/dev/null || echo "[run_extra_tools] WARNING: failed to unload."
done

echo "[run_extra_tools] Done. Results under $OUTPUT_ROOT/."
