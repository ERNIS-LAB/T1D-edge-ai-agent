#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# NO-TOOLS arm of the v2 benchmark (benchmark/tasks_v2.json / python -m benchmark).
#
# Instead of data-query tools, each patient's FULL dataset is dumped into the
# context window and the agent keeps only log_conclusion. python -m benchmark --no-tools
# also drops the tasks the agent can't do without tools (logging / memory / food),
# running only the ~58 it can answer from context. The JUDGE and chat/USER SIMULATOR
# are DeepSeek via OpenCode Zen, temperature 0 — identical to the with-tools arm.
#
# Local models load at a 200k context (the full dump is ~134k tokens; all listed
# models support up to 256k). The agent timeout is raised to 3200s because the
# large-prompt prefill is slow. Models that can't load at 200k are skipped. Kimi
# runs via the OpenCode Zen API (no local load, no context-length limit here).
set -u

CONTEXT_LENGTH=200000
AGENT_TIMEOUT=3200       # local models: large prefill at 200k can be slow
ZEN_AGENT_TIMEOUT=600    # remote API (kimi): fast prefill, fail fast on a hung request
PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
OUTPUT_ROOT="benchmark_results_v2_notools"

# Entries: "load_key|identifier". identifier = API id + output subdir.
MODELS=(
  "google/gemma-4-12b-qat|gemma-4-12b-qat"
  "google/gemma-4-26b-a4b-qat|gemma-4-26b-a4b-qat"
  "qwen3.6-35b-a3b|qwen3.6-35b-a3b"
  "qwen3.5-9b@q8_0|qwen3.5-9b-q8_0"
)

# Judge + user-simulator routing applied to every run: DeepSeek via OpenCode Zen.
ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

# --- Remote agent: Kimi via OpenCode Zen (run first — fast, no local load) -----
echo "=============================================="
echo "[run_all_v2_notools] Agent (zen, no-tools): kimi"
echo "=============================================="
lms unload --all 2>/dev/null  # free VRAM before the API run / first local load
if ! "$PYTHON" -m benchmark \
    --agent-provider zen --model kimi \
    --tasks-path "$TASKS" \
    --output "$OUTPUT_ROOT/kimi-agent" \
    --no-tools \
    --agent-timeout "$ZEN_AGENT_TIMEOUT" \
    "${ZEN_JUDGE_SIM[@]}"; then
  echo "[run_all_v2_notools] ERROR: benchmark failed for kimi (zen agent)."
fi

# --- Local agents (loaded via LM Studio) --------------------------------------
for entry in "${MODELS[@]}"; do
  load_key="${entry%%|*}"
  id="${entry##*|}"
  echo "=============================================="
  echo "[run_all_v2_notools] Agent (local, no-tools): $id  [$load_key]"
  echo "=============================================="

  echo "[run_all_v2_notools] Loading $load_key as '$id' (context $CONTEXT_LENGTH)..."
  if ! lms load "$load_key" --identifier "$id" -c "$CONTEXT_LENGTH" -y; then
    echo "[run_all_v2_notools] ERROR: failed to load $load_key at context $CONTEXT_LENGTH, skipping."
    continue
  fi

  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$OUTPUT_ROOT/$id" \
      --no-tools \
      --agent-timeout "$AGENT_TIMEOUT" \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[run_all_v2_notools] ERROR: benchmark failed for $id."
  fi

  echo "[run_all_v2_notools] Unloading $id..."
  lms unload --all 2>/dev/null || echo "[run_all_v2_notools] WARNING: failed to unload."
done

echo "[run_all_v2_notools] All no-tools runs done. Results under $OUTPUT_ROOT/."
