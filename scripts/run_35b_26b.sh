#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# (1) tools re-run of the NEW qwen3.6-35b-a3b (supersedes the broken old run), then
# (2) no-tools runs for qwen3.6-35b-a3b and gemma-4-26b-a4b-qat.
# Same config as the main sweeps: DeepSeek judge/sim, temp 0, 30k token cap.
set -u

PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

run_one() {
  local load_key="$1" id="$2" ctx="$3" outroot="$4" extra="$5"
  echo "=============================================="
  echo "[run_35b_26b] $id  [$load_key]  ctx=$ctx  ${extra:-tools}"
  echo "=============================================="
  lms unload --all 2>/dev/null
  if ! lms load "$load_key" --identifier "$id" -c "$ctx" -y; then
    echo "[run_35b_26b] ERROR: failed to load $load_key at ctx $ctx, skipping."
    return
  fi
  # shellcheck disable=SC2086
  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$outroot/$id" \
      $extra \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[run_35b_26b] ERROR: benchmark failed for $id ($outroot)."
  fi
  lms unload --all 2>/dev/null
}

# 1) TOOLS re-run: new qwen3.6-35b-a3b at 32k
run_one "qwen/qwen3.6-35b-a3b" "qwen3.6-35b-a3b" 32768 "benchmark_results_v2" ""

# 2) NO-TOOLS: qwen3.6-35b-a3b at 200k (3200s timeout via --agent-timeout)
run_one "qwen/qwen3.6-35b-a3b" "qwen3.6-35b-a3b" 200000 "benchmark_results_v2_notools" \
        "--no-tools --agent-timeout 3200"

# 3) NO-TOOLS: gemma-4-26b-a4b-qat at 200k
run_one "google/gemma-4-26b-a4b-qat" "gemma-4-26b-a4b-qat" 200000 "benchmark_results_v2_notools" \
        "--no-tools --agent-timeout 3200"

echo "[run_35b_26b] All done."
