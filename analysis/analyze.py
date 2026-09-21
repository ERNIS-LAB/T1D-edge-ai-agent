"""Analyze benchmark score results across models and generate comparison charts.

Reads the per-model `scores_*.jsonl` files in a results directory, joins them with
task metadata (difficulty/category/patient) from a tasks JSON file, and writes a set
of PNG charts plus a CSV summary into an `analysis_charts/` subdirectory.

Defaults target the multi-patient v2 suite:
    ./venv/bin/python python -m analysis.analyze
    # == python -m analysis.analyze --results-dir benchmark_results_v2 \
    #        --tasks-file benchmark/tasks_v2.json

To analyze the original single-patient suite instead:
    ./venv/bin/python python -m analysis.analyze \
        --results-dir benchmark_results --tasks-file benchmark_tasks.json

Score files are auto-discovered (latest run per model). The optional `--score-files`
flag still allows pinning an explicit `filename=label` set.
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent

# Metrics grouped by what they tell us about the model.
TOOL_METRICS = ["tool_correctness", "argument_correctness"]
# Both the loose pooled check and the strict per-field check describe conclusion quality.
CONCLUSION_METRICS = ["structured_conclusion", "keyed_conclusion"]
ALL_METRICS = [
    "tool_correctness",
    "argument_correctness",
    "numeric_groundedness",
    "structured_conclusion",
    "keyed_conclusion",
    "judge",
    "turn_completion",
    "per_turn_state",
    "logged_state",
    "evaluator_rejection",
]

DIFFICULTY_ORDER = ["easy", "medium", "hard", "unspecified"]
DIFFICULTY_COLORS = {
    "easy": "#4caf50",
    "medium": "#ff9800",
    "hard": "#f44336",
    "unspecified": "#9e9e9e",
}

# Filled in by main() from CLI args.
RESULTS_DIR = ROOT / "benchmark_results_v2"
TASKS_FILE = ROOT / "benchmark/tasks_v2.json"
OUT_DIR = RESULTS_DIR / "analysis_charts"

_SCORE_RE = re.compile(r"^scores_(?P<slug>.+)_(?P<ts>\d{8}T\d{6})\.jsonl$")


def load_task_meta():
    """task_id -> {difficulty, category, patient_id}. Tasks without a difficulty
    label (the older ohio_* set) are marked 'unspecified'."""
    tasks = json.loads(TASKS_FILE.read_text())
    meta = {}
    for t in tasks:
        meta[t["task_id"]] = {
            "difficulty": t.get("difficulty") or "unspecified",
            "category": t.get("category", "unknown"),
            "patient_id": t.get("patient_id", "shared"),
        }
    return meta


def discover_score_files():
    """Auto-discover scores_<slug>_<timestamp>.jsonl, keeping the latest run per
    model slug. Returns {label: path}."""
    latest: dict[str, tuple[str, Path]] = {}
    # Recurse so per-model subdirectory runs (RESULTS_DIR/<model>/scores_*.jsonl) are
    # found alongside any older top-level scores files.
    for path in RESULTS_DIR.rglob("scores_*.jsonl"):
        m = _SCORE_RE.match(path.name)
        if not m:
            continue
        slug, ts = m.group("slug"), m.group("ts")
        if slug not in latest or ts > latest[slug][0]:
            latest[slug] = (ts, path)
    return {slug: path for slug, (ts, path) in sorted(latest.items())}


def load_scores(score_files):
    """label -> list of record dicts (one per task)."""
    data = {}
    for label, path in score_files.items():
        path = Path(path)
        if not path.exists():
            print(f"WARNING: missing {path}, skipping")
            continue
        recs = [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]
        data[label] = recs
    return data


def is_failure(rec):
    """A task 'failed' (empty response) if any score detail mentions 'empty'."""
    for m in rec.get("scores", {}).values():
        if "empty" in str(m.get("detail", "")).lower():
            return True
    return False


def overall_score(rec):
    """Evenly-weighted overall score: the unweighted mean of every metric that
    was evaluated for this task, ignoring the task-defined `weights`."""
    vals = [
        m.get("score")
        for m in rec.get("scores", {}).values()
        if m.get("score") is not None
    ]
    return float(np.mean(vals)) if vals else 0.0


def weighted_score(rec):
    """The task-defined normalized score (respects per-task weights)."""
    v = rec.get("normalized_score")
    return float(v) if v is not None else overall_score(rec)


def metric_score(rec, metric):
    """Return the metric's score for a record, or None if not evaluated."""
    m = rec.get("scores", {}).get(metric)
    if m is None:
        return None
    return m.get("score")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return float(np.mean(xs)) if xs else float("nan")


# ----------------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------------


def chart_overall_distribution(scores, models):
    fig, axes = plt.subplots(
        1, len(models), figsize=(4.2 * len(models), 4.2), sharey=True
    )
    if len(models) == 1:
        axes = [axes]
    bins = np.linspace(0, 1, 11)
    for ax, model in zip(axes, models):
        vals = [overall_score(r) for r in scores[model]]
        ax.hist(vals, bins=bins, color="#3f51b5", edgecolor="white")
        mu = np.mean(vals)
        ax.axvline(
            mu, color="#e91e63", linestyle="--", linewidth=2, label=f"mean={mu:.2f}"
        )
        ax.set_title(model, fontsize=11)
        ax.set_xlabel("even-weighted score")
        ax.legend(fontsize=8)
    axes[0].set_ylabel("task count")
    fig.suptitle("Overall even-weighted score distribution per model", fontsize=13)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    save(fig, "01_overall_score_distribution.png")


def chart_mean_overall(scores, models):
    means = [np.mean([overall_score(r) for r in scores[m]]) for m in models]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars = ax.bar(models, means, color="#3f51b5")
    for b, v in zip(bars, means):
        ax.text(
            b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.3f}", ha="center", fontsize=9
        )
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean even-weighted score")
    ax.set_title("Mean overall benchmark score per model (even-weighted)")
    plt.xticks(rotation=15)
    fig.tight_layout()
    save(fig, "02_mean_overall_score.png")


def chart_score_by_difficulty(scores, models, meta):
    diffs = [
        d
        for d in DIFFICULTY_ORDER
        if any(
            meta.get(r["task_id"], {}).get("difficulty") == d
            for recs in scores.values()
            for r in recs
        )
    ]
    x = np.arange(len(diffs))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(9, 5))
    for i, model in enumerate(models):
        by_diff = defaultdict(list)
        for r in scores[model]:
            d = meta.get(r["task_id"], {}).get("difficulty", "unspecified")
            by_diff[d].append(overall_score(r))
        means = [np.mean(by_diff[d]) if by_diff[d] else 0 for d in diffs]
        ax.bar(x + i * width, means, width, label=model)
    ax.set_xticks(x + width * (len(models) - 1) / 2)
    ax.set_xticklabels(
        [
            f"{d}\n(n={sum(1 for v in meta.values() if v['difficulty'] == d)})"
            for d in diffs
        ]
    )
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean even-weighted score")
    ax.set_title("Score by task difficulty")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "03_score_by_difficulty.png")


def chart_tool_calling(scores, models):
    x = np.arange(len(TOOL_METRICS))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, model in enumerate(models):
        means = [
            mean([metric_score(r, mt) for r in scores[model]]) for mt in TOOL_METRICS
        ]
        bars = ax.bar(x + i * width, means, width, label=model)
        for b, v in zip(bars, means):
            ax.text(
                b.get_x() + b.get_width() / 2,
                v + 0.01,
                f"{v:.2f}",
                ha="center",
                fontsize=7,
            )
    ax.set_xticks(x + width * (len(models) - 1) / 2)
    ax.set_xticklabels(["tool selection\n(tool_correctness)", "argument\ncorrectness"])
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean score")
    ax.set_title("Tool-calling ability per model")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "04_tool_calling.png")


def chart_conclusions(scores, models):
    """Mean structured_conclusion (protocol) vs keyed_conclusion (per-field
    correctness) per model — the two now tell different stories."""
    x = np.arange(len(CONCLUSION_METRICS))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, model in enumerate(models):
        means = [
            mean([metric_score(r, mt) for r in scores[model]])
            for mt in CONCLUSION_METRICS
        ]
        bars = ax.bar(x + i * width, means, width, label=model)
        for b, v in zip(bars, means):
            if not np.isnan(v):
                ax.text(
                    b.get_x() + b.get_width() / 2,
                    v + 0.01,
                    f"{v:.2f}",
                    ha="center",
                    fontsize=7,
                )
    ax.set_xticks(x + width * (len(models) - 1) / 2)
    ax.set_xticklabels(
        ["structured_conclusion\n(valid output / right keys)", "keyed_conclusion\n(values correct)"]
    )
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean score")
    ax.set_title("Conclusion quality: output contract vs value correctness")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "05_logging_conclusions.png")


def chart_failures_by_model(scores, models):
    counts = [sum(1 for r in scores[m] if is_failure(r)) for m in models]
    totals = [len(scores[m]) for m in models]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    bars = ax.bar(models, counts, color="#f44336")
    for b, c, t in zip(bars, counts, totals):
        ax.text(
            b.get_x() + b.get_width() / 2,
            c + 0.2,
            f"{c}/{t}\n({c / t * 100:.0f}%)",
            ha="center",
            fontsize=9,
        )
    ax.set_ylabel("empty-response failures")
    ax.set_title("Failed tasks (empty response) per model")
    plt.xticks(rotation=15)
    fig.tight_layout()
    save(fig, "06_failures_by_model.png")


def chart_failures_by_task(scores, models, meta):
    task_ids = [r["task_id"] for r in scores[models[0]]]
    grid = np.zeros((len(task_ids), len(models)))
    for j, model in enumerate(models):
        fmap = {r["task_id"]: is_failure(r) for r in scores[model]}
        for i, tid in enumerate(task_ids):
            grid[i, j] = 1 if fmap.get(tid) else 0
    order = np.argsort(-grid.sum(axis=1))
    grid = grid[order]
    labels = [
        f"{task_ids[i]} [{meta.get(task_ids[i], {}).get('difficulty', '?')[:1]}]"
        for i in order
    ]
    fig, ax = plt.subplots(figsize=(8, max(6, 0.28 * len(task_ids))))
    ax.imshow(grid, aspect="auto", cmap="Reds", vmin=0, vmax=1)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(models, rotation=20, ha="right", fontsize=8)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels, fontsize=6)
    ax.set_title("Failed tasks (red = empty response)\n[e/m/h/u = difficulty]")
    fig.tight_layout()
    save(fig, "07_failures_by_task.png")


def chart_metric_heatmap(scores, models):
    metrics = [
        m
        for m in ALL_METRICS
        if any(metric_score(r, m) is not None for recs in scores.values() for r in recs)
    ]
    grid = np.array(
        [
            [mean([metric_score(r, m) for r in scores[model]]) for model in models]
            for m in metrics
        ]
    )
    fig, ax = plt.subplots(figsize=(1.6 * len(models) + 3, 0.6 * len(metrics) + 2))
    im = ax.imshow(grid, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(models, rotation=20, ha="right", fontsize=8)
    ax.set_yticks(range(len(metrics)))
    ax.set_yticklabels(metrics, fontsize=9)
    for i in range(len(metrics)):
        for j in range(len(models)):
            v = grid[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label="mean score")
    ax.set_title("Mean score per metric x model")
    fig.tight_layout()
    save(fig, "08_metric_heatmap.png")


def chart_category_heatmap(scores, models, meta):
    cats = sorted(
        {
            meta.get(r["task_id"], {}).get("category", "unknown")
            for recs in scores.values()
            for r in recs
        }
    )
    grid = np.full((len(cats), len(models)), np.nan)
    for j, model in enumerate(models):
        by_cat = defaultdict(list)
        for r in scores[model]:
            c = meta.get(r["task_id"], {}).get("category", "unknown")
            by_cat[c].append(overall_score(r))
        for i, c in enumerate(cats):
            if by_cat[c]:
                grid[i, j] = np.mean(by_cat[c])
    fig, ax = plt.subplots(figsize=(1.6 * len(models) + 3, 0.5 * len(cats) + 2))
    im = ax.imshow(grid, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(models, rotation=20, ha="right", fontsize=8)
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels(cats, fontsize=8)
    for i in range(len(cats)):
        for j in range(len(models)):
            v = grid[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, label="mean even-weighted score")
    ax.set_title("Mean even-weighted score per category x model")
    fig.tight_layout()
    save(fig, "09_category_heatmap.png")


def chart_score_by_patient(scores, models, meta):
    """Grouped bars: mean even-weighted score per patient, per model. Reveals
    cross-patient generalization (v2's multi-patient design). Patient comes from
    the score record's patient_id (tagged by benchmark_v2), falling back to task
    metadata for older runs."""
    def pid_of(rec):
        return rec.get("patient_id") or meta.get(rec["task_id"], {}).get(
            "patient_id", "shared"
        )

    patients = sorted(
        {pid_of(r) for recs in scores.values() for r in recs},
        key=lambda p: (p == "shared", p),
    )
    if len(patients) <= 1:
        return  # single-patient suite — chart adds nothing

    x = np.arange(len(patients))
    width = 0.8 / len(models)
    fig, ax = plt.subplots(figsize=(max(8, 1.5 * len(patients)), 5))
    for i, model in enumerate(models):
        by_pid = defaultdict(list)
        for r in scores[model]:
            by_pid[pid_of(r)].append(overall_score(r))
        means = [np.mean(by_pid[p]) if by_pid[p] else 0 for p in patients]
        bars = ax.bar(x + i * width, means, width, label=model)
        for b, v in zip(bars, means):
            ax.text(
                b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.2f}", ha="center", fontsize=7
            )
    ax.set_xticks(x + width * (len(models) - 1) / 2)
    ax.set_xticklabels(
        [f"{p}\n(n={sum(1 for r in scores[models[0]] if pid_of(r) == p)})" for p in patients]
    )
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean even-weighted score")
    ax.set_title("Score by patient (cross-patient generalization)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    save(fig, "10_score_by_patient.png")


def write_summary_csv(scores, models, meta):
    path = OUT_DIR / "summary_table.csv"
    metrics = [
        m
        for m in ALL_METRICS
        if any(metric_score(r, m) is not None for recs in scores.values() for r in recs)
    ]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        header = [
            "model",
            "n_tasks",
            "mean_overall_even",
            "mean_overall_weighted",
            "failures",
            "failure_rate",
        ] + [f"mean_{m}" for m in metrics]
        w.writerow(header)
        for model in models:
            recs = scores[model]
            fails = sum(1 for r in recs if is_failure(r))
            row = [
                model,
                len(recs),
                f"{np.mean([overall_score(r) for r in recs]):.4f}",
                f"{np.mean([weighted_score(r) for r in recs]):.4f}",
                fails,
                f"{fails / len(recs):.3f}",
            ]
            row += [f"{mean([metric_score(r, m) for r in recs]):.4f}" for m in metrics]
            w.writerow(row)
    print(f"  wrote {path.name}")


def save(fig, name):
    path = OUT_DIR / name
    fig.savefig(path, dpi=130)
    plt.close(fig)
    print(f"  wrote {name}")


def _build_parser():
    p = argparse.ArgumentParser(description="Analyze benchmark score results")
    p.add_argument(
        "--results-dir",
        default="benchmark_results_v2",
        help="Directory containing scores_*.jsonl (default: benchmark_results_v2)",
    )
    p.add_argument(
        "--tasks-file",
        default="benchmark/tasks_v2.json",
        help="Tasks JSON for metadata (default: benchmark/tasks_v2.json)",
    )
    p.add_argument(
        "--score-files",
        nargs="+",
        default=None,
        help="Optional explicit 'filename=label' pairs (overrides auto-discovery)",
    )
    return p


def main():
    global RESULTS_DIR, TASKS_FILE, OUT_DIR
    args = _build_parser().parse_args()
    RESULTS_DIR = (ROOT / args.results_dir).resolve()
    TASKS_FILE = (ROOT / args.tasks_file).resolve()
    OUT_DIR = RESULTS_DIR / "analysis_charts"
    OUT_DIR.mkdir(exist_ok=True)

    if args.score_files:
        score_files = {}
        for spec in args.score_files:
            fname, _, label = spec.partition("=")
            score_files[label or fname] = RESULTS_DIR / fname
    else:
        score_files = discover_score_files()

    if not score_files:
        raise SystemExit(f"No scores_*.jsonl found in {RESULTS_DIR}")

    meta = load_task_meta()
    scores = load_scores(score_files)
    models = list(scores.keys())
    if not models:
        raise SystemExit("No score files loaded.")

    print(f"Loaded {len(models)} model run(s) from {RESULTS_DIR}. Charts -> {OUT_DIR}/")
    for m in models:
        print(f"  - {m}: {len(scores[m])} tasks")

    chart_overall_distribution(scores, models)
    chart_mean_overall(scores, models)
    chart_score_by_difficulty(scores, models, meta)
    chart_tool_calling(scores, models)
    chart_conclusions(scores, models)
    chart_failures_by_model(scores, models)
    chart_failures_by_task(scores, models, meta)
    chart_metric_heatmap(scores, models)
    chart_category_heatmap(scores, models, meta)
    chart_score_by_patient(scores, models, meta)
    write_summary_csv(scores, models, meta)

    print("\n=== Quick digest ===")
    for model in models:
        recs = scores[model]
        fails = sum(1 for r in recs if is_failure(r))
        print(
            f"{model:24s} even={np.mean([overall_score(r) for r in recs]):.3f}  "
            f"weighted={np.mean([weighted_score(r) for r in recs]):.3f}  "
            f"fails={fails}/{len(recs)}  "
            f"tool={mean([metric_score(r, 'tool_correctness') for r in recs]):.2f}  "
            f"keyed={mean([metric_score(r, 'keyed_conclusion') for r in recs]):.2f}"
        )


if __name__ == "__main__":
    main()
