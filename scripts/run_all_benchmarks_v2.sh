#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# WITH-TOOLS arm of the v2 benchmark (benchmark/tasks_v2.json / python -m benchmark).
#
# Each agent-under-test is loaded into LM Studio in turn at a 32k context and runs
# the full v2 task suite WITH the data-query tools. The JUDGE and chat/USER
# SIMULATOR are always DeepSeek on the OpenCode Zen endpoint, so only the agent
# changes between runs. Temperature is fixed at 0 (config.TEMPERATURE).
#
# Models are loaded by their LM Studio model-key and given a clean --identifier
# that (a) becomes the API model id and (b) names the output dir — so the quant is
# captured in the results path for later analysis. Note: LM Studio's CLI can only
# load a model-key's *selected* variant, so the multi-variant Gemma MLX models run
# at whatever quant is selected in the GUI (e2b -> 4bit, e4b -> 8bit at setup time).
set -u

CONTEXT_LENGTH=32768
PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
OUTPUT_ROOT="benchmark_results_v2"

# Entries: "load_key|identifier". identifier = API id + output subdir (encodes quant).
MODELS=(
  "google/gemma-4-e2b|gemma-4-e2b-4bit"
  "google/gemma-4-e4b|gemma-4-e4b-8bit"
  "google/gemma-4-26b-a4b-qat|gemma-4-26b-a4b-qat"
  "qwen3.5-4b@bf16|qwen3.5-4b-bf16"
  "qwen3.5-4b@q8_0|qwen3.5-4b-q8_0"
  "qwen3.5-4b@q4_0|qwen3.5-4b-q4_0"
  "qwen3.5-9b@q8_0|qwen3.5-9b-q8_0"
  "qwen3.5-9b@q4_0|qwen3.5-9b-q4_0"
)

# Judge + user-simulator routing applied to every run: DeepSeek via OpenCode Zen.
ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

for entry in "${MODELS[@]}"; do
  load_key="${entry%%|*}"
  id="${entry##*|}"
  echo "=============================================="
  echo "[run_all_v2] Agent (local, tools): $id  [$load_key]"
  echo "=============================================="

  echo "[run_all_v2] Loading $load_key as '$id' (context $CONTEXT_LENGTH)..."
  if ! lms load "$load_key" --identifier "$id" -c "$CONTEXT_LENGTH" -y; then
    echo "[run_all_v2] ERROR: failed to load $load_key, skipping."
    continue
  fi

  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$OUTPUT_ROOT/$id" \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[run_all_v2] ERROR: benchmark failed for $id."
  fi

  echo "[run_all_v2] Unloading $id..."
  lms unload --all 2>/dev/null || echo "[run_all_v2] WARNING: failed to unload."
done

echo "[run_all_v2] All tools runs done. Results under $OUTPUT_ROOT/."
