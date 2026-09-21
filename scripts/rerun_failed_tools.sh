#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# Re-run the 3 with-tools models that crashed in the first sweep on the evaluator's
# unhandled bad-date bug (now fixed in benchmark_evaluator.score_argument_correctness).
# Same config as run_all_benchmarks_v2.sh (32k context, DeepSeek judge/sim, temp 0).
set -u

CONTEXT_LENGTH=32768
PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
OUTPUT_ROOT="benchmark_results_v2"

MODELS=(
  "qwen3.5-4b@q4_0|qwen3.5-4b-q4_0"
  "qwen3.5-9b@q8_0|qwen3.5-9b-q8_0"
  "qwen3.5-9b@q4_0|qwen3.5-9b-q4_0"
)

ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

for entry in "${MODELS[@]}"; do
  load_key="${entry%%|*}"
  id="${entry##*|}"
  echo "=============================================="
  echo "[rerun_tools] Agent (local, tools): $id  [$load_key]"
  echo "=============================================="

  echo "[rerun_tools] Loading $load_key as '$id' (context $CONTEXT_LENGTH)..."
  if ! lms load "$load_key" --identifier "$id" -c "$CONTEXT_LENGTH" -y; then
    echo "[rerun_tools] ERROR: failed to load $load_key, skipping."
    continue
  fi

  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$OUTPUT_ROOT/$id" \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[rerun_tools] ERROR: benchmark failed for $id."
  fi

  echo "[rerun_tools] Unloading $id..."
  lms unload --all 2>/dev/null || echo "[rerun_tools] WARNING: failed to unload."
done

echo "[rerun_tools] Done. Results under $OUTPUT_ROOT/."
