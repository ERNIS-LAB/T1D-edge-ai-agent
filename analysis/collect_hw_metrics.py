# collect_hw_metrics
# One-off script: run 5 benchmark-v2 prompts against an edge Ollama host for a
# single edge model and record hardware metrics (TTFT, prefill/decode TPS, load
# time). Streaming is used so time-to-first-token is a real wall-clock measure;
# the final stream chunk carries Ollama's own duration counters.
#
# Usage: python3 -m analysis.collect_hw_metrics <model> [num_ctx]
# Results are appended to results/hardware_metrics.json (one entry per prompt).
#
# Point HW_METRICS_HOST at the Ollama host being measured (the edge device),
# e.g. HW_METRICS_HOST=http://192.168.1.50:11434

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HOST = os.getenv("HW_METRICS_HOST", "http://127.0.0.1:11434")
OUT_PATH = str(ROOT / "results" / "hardware_metrics.json")
NUM_PREDICT = 256  # bound decode length so runs stay comparable across models

TASK_IDS = [
    "v2_540_lookup_001",   # easy lookup
    "v2_540_meals_001",    # easy summary
    "v2_540_tir_001",      # medium time-in-range
    "v2_540_ea1c_001",     # hard summary (estimated A1C)
    "v2_food_gi_004",      # medium food/glycemic (patient-independent)
]


def load_prompts():
    tasks_path = ROOT / "benchmark" / "tasks_v2.json"
    tasks = {t["task_id"]: t for t in json.loads(tasks_path.read_text())}
    return [(tid, tasks[tid]["prompt"]) for tid in TASK_IDS]


def run_prompt(model, prompt, num_ctx=None):
    # curl -sN is used instead of urllib because urllib's buffered line
    # iteration does not surface stream chunks promptly, which breaks the
    # wall-clock time-to-first-token measurement.
    options = {"temperature": 0.0, "num_predict": NUM_PREDICT}
    if num_ctx:
        options["num_ctx"] = num_ctx
    body = json.dumps(
        {"model": model, "prompt": prompt, "stream": True, "options": options}
    )
    proc = subprocess.Popen(
        ["curl", "-sN", "--max-time", "600", f"{HOST}/api/generate", "-d", body],
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    t0 = time.monotonic()
    ttft = None
    final = None
    assert proc.stdout is not None
    for line in proc.stdout:
        if not line.strip():
            continue
        chunk = json.loads(line)
        if ttft is None and (chunk.get("response") or chunk.get("thinking")):
            ttft = time.monotonic() - t0
        if chunk.get("done"):
            final = chunk
    wall = time.monotonic() - t0
    proc.wait()
    if final is None:
        raise RuntimeError("stream ended without a final chunk (HTTP error?)")
    ns = 1e9
    return {
        "ttft_wall_s": round(ttft, 3) if ttft else None,
        "wall_s": round(wall, 3),
        "load_s": round(final.get("load_duration", 0) / ns, 3),
        "prompt_tokens": final.get("prompt_eval_count"),
        "prefill_s": round(final.get("prompt_eval_duration", 0) / ns, 3),
        "prefill_tps": round(
            final["prompt_eval_count"] / (final["prompt_eval_duration"] / ns), 1
        )
        if final.get("prompt_eval_duration")
        else None,
        "gen_tokens": final.get("eval_count"),
        "decode_s": round(final.get("eval_duration", 0) / ns, 3),
        "decode_tps": round(final["eval_count"] / (final["eval_duration"] / ns), 2)
        if final.get("eval_duration")
        else None,
        "total_s": round(final.get("total_duration", 0) / ns, 3),
    }


def main():
    model = sys.argv[1]
    num_ctx = int(sys.argv[2]) if len(sys.argv) > 2 else None
    try:
        results = json.load(open(OUT_PATH))
    except FileNotFoundError:
        results = []

    for tid, prompt in load_prompts():
        print(f"[{model}] {tid} ...", flush=True)
        try:
            m = run_prompt(model, prompt, num_ctx)
        except Exception as e:
            print(f"  FAILED: {e}", flush=True)
            results.append({"model": model, "task_id": tid, "error": str(e)})
            json.dump(results, open(OUT_PATH, "w"), indent=1)
            sys.exit(1)
        m.update({"model": model, "task_id": tid, "num_ctx": num_ctx})
        results.append(m)
        print(
            f"  ttft={m['ttft_wall_s']}s load={m['load_s']}s "
            f"prefill={m['prefill_tps']}tps decode={m['decode_tps']}tps "
            f"({m['gen_tokens']} tok in {m['decode_s']}s)",
            flush=True,
        )
        json.dump(results, open(OUT_PATH, "w"), indent=1)

    # Unload the model so the next one has the full memory budget.
    urllib.request.urlopen(
        urllib.request.Request(
            f"{HOST}/api/generate",
            data=json.dumps({"model": model, "keep_alive": 0}).encode(),
            headers={"Content-Type": "application/json"},
        ),
        timeout=60,
    ).read()
    print(f"[{model}] done, unloaded.", flush=True)


if __name__ == "__main__":
    main()
