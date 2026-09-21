#!/usr/bin/env bash

# Run from the repository root regardless of where this script is invoked.
cd "$(dirname "$0")/.." || exit 1
# Experiment 4 (RQ4): edge-target models under a REDUCED context window, on the
# tool-enabled suite. Same params as the other tools runs (DeepSeek judge/sim,
# temp 0, 30k token cap); only the context window changes. Each model also has a
# full-context (32k) counterpart in benchmark_results_v2/, so the pair shows the
# effect of context truncation. Loaded footprint is recorded for the memory column.
set -u

PYTHON=./venv/bin/python
TASKS="benchmark/tasks_v2.json"
OUTPUT_ROOT="benchmark_results_v2_edge"
MEM_CSV="$OUTPUT_ROOT/edge_memory.csv"

# entries: "load_key|identifier|context"
MODELS=(
  "qwen3.5-9b@iq3_xxs|qwen3.5-9b-iq3_xxs|4096"
  "qwen3.5-4b@q4_0|qwen3.5-4b-q4_0|8192"
  "google/gemma-4-e2b|gemma-4-e2b-4bit|8192"
)

ZEN_JUDGE_SIM=(
  --judge-provider zen --judge-model deepseek
  --simulator-provider zen --simulator-model deepseek
)

mkdir -p "$OUTPUT_ROOT"
echo "identifier,context,footprint" > "$MEM_CSV"

for entry in "${MODELS[@]}"; do
  load_key="${entry%%|*}"; rest="${entry#*|}"; id="${rest%%|*}"; ctx="${rest##*|}"
  echo "=============================================="
  echo "[exp4_edge] $id  [$load_key]  ctx=$ctx"
  echo "=============================================="
  lms unload --all 2>/dev/null
  if ! lms load "$load_key" --identifier "$id" -c "$ctx" -y; then
    echo "[exp4_edge] ERROR: failed to load $load_key at ctx $ctx, skipping."
    continue
  fi

  # Record loaded footprint (SIZE field from lms ps: value + unit).
  foot=$(lms ps 2>/dev/null | awk -v id="$id" '$1==id {print $4" "$5}')
  echo "$id,$ctx,$foot" >> "$MEM_CSV"
  echo "[exp4_edge] footprint: ${foot:-unknown}"

  if ! "$PYTHON" -m benchmark \
      --model "$id" \
      --tasks-path "$TASKS" \
      --output "$OUTPUT_ROOT/$id" \
      "${ZEN_JUDGE_SIM[@]}"; then
    echo "[exp4_edge] ERROR: benchmark failed for $id."
  fi

  lms unload --all 2>/dev/null
done

echo "[exp4_edge] Done. Results under $OUTPUT_ROOT/ (memory in $MEM_CSV)."
