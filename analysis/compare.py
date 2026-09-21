"""Cross-cutting comparison of benchmark runs: model size, quantization, and
tools vs no-tools.

Unlike python -m analysis.analyze (which charts models within a single results dir),
this reads BOTH arms — the with-tools runs in `benchmark_results_v2/` and the
no-tools runs in `benchmark_results_v2_notools/` — parses each run's model id into
(family, size, quant, condition), and produces comparison charts + a tidy CSV.

    ./venv/bin/python python -m analysis.compare
    # tools-dir=benchmark_results_v2  notools-dir=benchmark_results_v2_notools

Scoring metrics reported per run:
  - mean_keyed:      mean keyed_conclusion (per-field answer correctness). This is
                     the fairest cross-condition metric — it exists in both arms and
                     is unaffected by the tool/argument dims that no-tools drops.
  - mean_overall:    even-weighted mean of every evaluated metric (matches
                     analyze_benchmarks' "overall").
  - mean_normalized: the task-defined normalized_score (weights differ by arm, so
                     compare this within an arm, not across).
"""

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from analysis.analyze import is_failure, mean, metric_score, overall_score, weighted_score

ROOT = Path(__file__).resolve().parent.parent
_SCORE_RE = re.compile(r"^scores_(?P<slug>.+)_(?P<ts>\d{8}T\d{6})\.jsonl$")

FAMILY_COLORS = {"Qwen": "#3f51b5", "Gemma": "#e5732a", "Kimi": "#2e9e5b", "?": "#9e9e9e"}
CONDITION_MARKERS = {"tools": "o", "no_tools": "s"}


# ---------------------------------------------------------------------------
# Parsing a run's model id -> (family, size_b, quant, quant_bits)
# ---------------------------------------------------------------------------


def parse_model(slug: str) -> dict:
    """Best-effort parse of a model dir/slug into comparison dimensions."""
    s = slug.lower().replace("google_", "").replace("google-", "")

    if "kimi" in s:
        family = "Kimi"
    elif "gemma" in s:
        family = "Gemma"
    elif "qwen" in s:
        family = "Qwen"
    else:
        family = "?"

    # Size (total params, in billions). Gemma "eNb" effective sizes map to N.
    size_b = None
    m = re.search(r"e(\d+)b", s)  # gemma-4-e2b / e4b
    if m:
        size_b = float(m.group(1))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)b(?![a-z])", s)  # 4b, 9b, 12b, 26b, 35b
        if m:
            size_b = float(m.group(1))

    # Quantization -> (label, bit-width). qat is 4-bit quantization-aware training.
    # The MoE "aNb" suffix (a4b/a3b) denotes active params, not quant; both of our
    # MoE builds (gemma-26b-a4b, qwen-35b-a3b) ship as 4-bit, so map them to q4.
    # Some identifiers don't encode the quant at all — override those explicitly.
    _QUANT_OVERRIDE = {"qwen3-6-27b": ("q4", 4.0)}  # qwen/qwen3.6-27b is Q4_K_M
    if s in _QUANT_OVERRIDE:
        quant, bits = _QUANT_OVERRIDE[s]
        return {"family": family, "size_b": size_b, "quant": quant, "quant_bits": bits}

    quant, bits = "?", None
    for pat, lab, b in [
        (r"bf16", "bf16", 16.0),
        (r"q8|8bit|q8_0", "q8", 8.0),
        (r"q6|q6_k", "q6", 6.0),
        (r"iq3|q3", "q3", 3.0),
        (r"q4|4bit|q4_0|qat|a4b|a3b", "q4", 4.0),
    ]:
        if re.search(pat, s):
            quant, bits = lab, b
            break
    if family == "Kimi":
        quant, bits = "api", None

    return {"family": family, "size_b": size_b, "quant": quant, "quant_bits": bits}


# ---------------------------------------------------------------------------
# Discovery + loading across both arms
# ---------------------------------------------------------------------------


def discover(results_dir: Path, condition: str) -> dict:
    """Return {slug: {'condition','path', ...parsed}} for the latest run per model."""
    latest: dict[str, tuple[str, Path]] = {}
    # Recurse: new runs write to per-model subdirs (results_dir/<id>/scores_*.jsonl),
    # while some older runs sit at the top level. rglob covers both.
    for path in results_dir.rglob("scores_*.jsonl"):
        m = _SCORE_RE.match(path.name)
        if not m:
            continue
        slug, ts = m.group("slug"), m.group("ts")
        if slug not in latest or ts > latest[slug][0]:
            latest[slug] = (ts, path)
    runs = {}
    for slug, (_ts, path) in latest.items():
        key = f"{slug} [{condition}]"
        runs[key] = {"slug": slug, "condition": condition, "path": path, **parse_model(slug)}
    return runs


def load_records(path: Path) -> list:
    return [
        __import__("json").loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def summarize(runs: dict) -> list:
    """Attach per-run aggregate metrics to each parsed run."""
    rows = []
    for key, info in runs.items():
        recs = load_records(info["path"])
        if not recs:
            continue
        row = dict(info)
        row["key"] = key
        row["n_tasks"] = len(recs)
        row["failures"] = sum(1 for r in recs if is_failure(r))
        row["mean_keyed"] = mean([metric_score(r, "keyed_conclusion") for r in recs])
        row["mean_structured"] = mean(
            [metric_score(r, "structured_conclusion") for r in recs]
        )
        # Primary comparison metrics: numeric groundedness and the LLM judge.
        row["mean_numeric"] = mean(
            [metric_score(r, "numeric_groundedness") for r in recs]
        )
        row["mean_judge"] = mean([metric_score(r, "judge") for r in recs])
        row["mean_tool"] = mean([metric_score(r, "tool_correctness") for r in recs])
        row["mean_overall"] = float(np.mean([overall_score(r) for r in recs]))
        row["mean_normalized"] = float(np.mean([weighted_score(r) for r in recs]))
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _nice(v):
    return "n/a" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.2f}"


def chart_score_vs_size(rows, out_dir, metric="mean_keyed"):
    """Tools arm only. Faint scatter of every (size, quant) point plus a bold
    best-quant-per-size trend line per family — clean instead of zig-zagging
    through multiple quants at the same size."""
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for fam in ("Qwen", "Gemma"):
        pts = [
            r
            for r in rows
            if r["condition"] == "tools" and r["family"] == fam and r["size_b"]
            and not np.isnan(r[metric])
        ]
        if not pts:
            continue
        # faint individual points (one per quant)
        ax.scatter(
            [r["size_b"] for r in pts], [r[metric] for r in pts],
            color=FAMILY_COLORS[fam], alpha=0.35, s=30,
        )
        for r in pts:
            ax.annotate(r["quant"], (r["size_b"], r[metric]), fontsize=6,
                        xytext=(0, 4), textcoords="offset points", ha="center", alpha=0.6)
        # bold trend through the best quant at each size
        best = {}
        for r in pts:
            s = r["size_b"]
            if s not in best or r[metric] > best[s][metric]:
                best[s] = r
        xs = sorted(best)
        ax.plot([x for x in xs], [best[x][metric] for x in xs], marker="o",
                color=FAMILY_COLORS[fam], label=f"{fam} (best quant/size)", linewidth=2)
    ax.set_xlabel("model size (B params, total)")
    ax.set_ylabel(metric)
    ax.set_ylim(0, 1)
    ax.set_title(f"Score vs model size — tools arm  ({metric})")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _save(fig, out_dir, f"C1_score_vs_size__{metric}.png")


def chart_score_vs_quant(rows, out_dir, metric="mean_keyed"):
    """Grouped bars: for each (family,size) with >1 quant in an arm, score per quant."""
    quant_order = ["q3", "q4", "q6", "q8", "bf16"]
    groups = defaultdict(dict)  # (family,size,condition) -> {quant: score}
    for r in rows:
        if r["size_b"] is None or r["quant"] not in quant_order:
            continue
        groups[(r["family"], r["size_b"], r["condition"])][r["quant"]] = r[metric]
    groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not groups:
        return
    labels = [f"{f} {int(s)}B\n({c})" for (f, s, c) in groups]
    x = np.arange(len(quant_order))
    width = 0.8 / max(1, len(groups))
    fig, ax = plt.subplots(figsize=(max(8, 1.5 * len(groups) + 4), 5))
    for i, (gk, qmap) in enumerate(groups.items()):
        ys = [qmap.get(q, np.nan) for q in quant_order]
        bars = ax.bar(x + i * width, ys, width, label=labels[i])
        for b, v in zip(bars, ys):
            if not np.isnan(v):
                ax.text(
                    b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.2f}",
                    ha="center", fontsize=7,
                )
    ax.set_xticks(x + width * (len(groups) - 1) / 2)
    ax.set_xticklabels(quant_order)
    ax.set_xlabel("quantization (lower bits -> left)")
    ax.set_ylabel(metric)
    ax.set_ylim(0, 1)
    ax.set_title(f"Score vs quantization  ({metric})")
    ax.legend(fontsize=8)
    fig.tight_layout()
    _save(fig, out_dir, f"C2_score_vs_quant__{metric}.png")


def chart_tools_vs_notools(rows, out_dir, metric="mean_keyed"):
    """Paired bars for models present in BOTH arms (matched by slug)."""
    by_slug = defaultdict(dict)
    for r in rows:
        by_slug[r["slug"]][r["condition"]] = r[metric]
    paired = {s: d for s, d in by_slug.items() if "tools" in d and "no_tools" in d}
    fig, ax = plt.subplots(figsize=(max(7, 1.4 * (len(paired) + 1)), 5))
    if paired:
        slugs = sorted(paired)
        x = np.arange(len(slugs))
        ax.bar(x - 0.2, [paired[s]["tools"] for s in slugs], 0.4, label="tools", color="#3f51b5")
        ax.bar(x + 0.2, [paired[s]["no_tools"] for s in slugs], 0.4, label="no-tools", color="#e5732a")
        ax.set_xticks(x)
        ax.set_xticklabels(slugs, rotation=15, ha="right", fontsize=8)
    else:
        # No model overlaps both arms -> fall back to arm-level means.
        for i, cond in enumerate(("tools", "no_tools")):
            vals = [r[metric] for r in rows if r["condition"] == cond and not np.isnan(r[metric])]
            ax.bar(i, np.mean(vals) if vals else 0, 0.6,
                   color=["#3f51b5", "#e5732a"][i], label=cond)
        ax.set_xticks([0, 1])
        ax.set_xticklabels(["tools (all)", "no-tools (all)"])
    ax.set_ylim(0, 1)
    ax.set_ylabel(metric)
    ax.set_title(f"Tools vs no-tools  ({metric})")
    ax.legend(fontsize=8)
    fig.tight_layout()
    _save(fig, out_dir, f"C3_tools_vs_notools__{metric}.png")


def chart_leaderboard(rows, out_dir, metric):
    """Horizontal ranked bars of every tools-arm model, coloured by family."""
    pts = sorted(
        [r for r in rows if r["condition"] == "tools" and not np.isnan(r[metric])],
        key=lambda r: r[metric],
    )
    if not pts:
        return
    labels = [f"{r['slug']}" for r in pts]
    vals = [r[metric] for r in pts]
    colors = [FAMILY_COLORS[r["family"]] for r in pts]
    fig, ax = plt.subplots(figsize=(9, 0.42 * len(pts) + 1.5))
    ax.barh(range(len(pts)), vals, color=colors)
    ax.set_yticks(range(len(pts)))
    ax.set_yticklabels(labels, fontsize=8)
    for i, v in enumerate(vals):
        ax.text(v + 0.005, i, f"{v:.2f}", va="center", fontsize=7)
    ax.set_xlim(0, 1)
    ax.set_xlabel(metric)
    ax.set_title(f"Model leaderboard — tools arm  ({metric})")
    handles = [Rectangle((0, 0), 1, 1, color=c) for c in FAMILY_COLORS.values()]
    ax.legend(handles, FAMILY_COLORS.keys(), fontsize=8, loc="lower right")
    fig.tight_layout()
    _save(fig, out_dir, f"C4_leaderboard__{metric}.png")


def chart_family_means(rows, out_dir, metric):
    """Mean metric per family (tools arm) — averages each family's models."""
    fams = ["Qwen", "Gemma", "Kimi"]
    means, errs, ns = [], [], []
    for fam in fams:
        vals = [
            r[metric]
            for r in rows
            if r["condition"] == "tools" and r["family"] == fam and not np.isnan(r[metric])
        ]
        means.append(np.mean(vals) if vals else np.nan)
        errs.append(np.std(vals) if len(vals) > 1 else 0.0)
        ns.append(len(vals))
    fig, ax = plt.subplots(figsize=(6.5, 5))
    bars = ax.bar(fams, means, yerr=errs, capsize=5,
                  color=[FAMILY_COLORS[f] for f in fams])
    for b, v, n in zip(bars, means, ns):
        if not np.isnan(v):
            ax.text(b.get_x() + b.get_width() / 2, v + 0.01, f"{v:.2f}\n(n={n})",
                    ha="center", fontsize=8)
    ax.set_ylim(0, 1)
    ax.set_ylabel(metric)
    ax.set_title(f"Family mean — tools arm  ({metric})\n(error bars = std across models)")
    fig.tight_layout()
    _save(fig, out_dir, f"C5_family_means__{metric}.png")


def chart_quant_bits_scatter(rows, out_dir, metric):
    """Score vs quant bit-width across all tools models, coloured by family —
    shows whether higher precision actually helps on average."""
    pts = [
        r for r in rows
        if r["condition"] == "tools" and r["quant_bits"] and not np.isnan(r[metric])
    ]
    if not pts:
        return
    fig, ax = plt.subplots(figsize=(8, 5.5))
    for fam in ("Qwen", "Gemma"):
        fp = [r for r in pts if r["family"] == fam]
        if fp:
            ax.scatter([r["quant_bits"] for r in fp], [r[metric] for r in fp],
                       color=FAMILY_COLORS[fam], label=fam, s=55, alpha=0.8)
            for r in fp:
                ax.annotate(f"{int(r['size_b'])}B" if r["size_b"] else "",
                            (r["quant_bits"], r[metric]), fontsize=6,
                            xytext=(4, 2), textcoords="offset points")
    ax.set_xticks([3, 4, 6, 8, 16])
    ax.set_xlabel("quantization (bits per weight)")
    ax.set_ylabel(metric)
    ax.set_ylim(0, 1)
    ax.set_title(f"Score vs quantization precision — tools arm  ({metric})")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _save(fig, out_dir, f"C6_quant_bits_scatter__{metric}.png")


def chart_numeric_vs_judge(rows, out_dir):
    """Per-model scatter of numeric-groundedness (x) vs judge (y), tools arm —
    reveals 'fluent but wrong' (high judge, low numeric) vs grounded models."""
    pts = [
        r for r in rows
        if r["condition"] == "tools"
        and not np.isnan(r["mean_numeric"]) and not np.isnan(r["mean_judge"])
    ]
    if not pts:
        return
    fig, ax = plt.subplots(figsize=(8, 7))
    for fam in ("Qwen", "Gemma", "Kimi"):
        fp = [r for r in pts if r["family"] == fam]
        if fp:
            ax.scatter([r["mean_numeric"] for r in fp], [r["mean_judge"] for r in fp],
                       color=FAMILY_COLORS[fam], label=fam, s=60, alpha=0.8)
    for r in pts:
        ax.annotate(r["slug"].replace("qwen3-5-", "q").replace("gemma-4-", "g"),
                    (r["mean_numeric"], r["mean_judge"]), fontsize=6,
                    xytext=(4, 3), textcoords="offset points")
    lim = [0.3, 1.0]
    ax.plot(lim, lim, "--", color="grey", alpha=0.5, label="judge = numeric")
    ax.set_xlim(*lim)
    ax.set_ylim(*lim)
    ax.set_xlabel("numeric groundedness (accuracy)")
    ax.set_ylabel("judge (prose quality)")
    ax.set_title("Fluent vs grounded — tools arm\n(above line = writes better than it computes)")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _save(fig, out_dir, "C7_numeric_vs_judge.png")


def chart_family_size_heatmap(rows, out_dir, metric):
    """Family x size grid of mean metric (tools arm); cell = mean over quants."""
    fams = ["Qwen", "Gemma"]
    sizes = sorted({r["size_b"] for r in rows if r["condition"] == "tools" and r["size_b"]})
    grid = np.full((len(fams), len(sizes)), np.nan)
    for i, fam in enumerate(fams):
        for j, s in enumerate(sizes):
            vals = [
                r[metric] for r in rows
                if r["condition"] == "tools" and r["family"] == fam
                and r["size_b"] == s and not np.isnan(r[metric])
            ]
            if vals:
                grid[i, j] = np.mean(vals)
    fig, ax = plt.subplots(figsize=(1.1 * len(sizes) + 2, 3))
    im = ax.imshow(grid, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax.set_xticks(range(len(sizes)))
    ax.set_xticklabels([f"{int(s)}B" for s in sizes])
    ax.set_yticks(range(len(fams)))
    ax.set_yticklabels(fams)
    for i in range(len(fams)):
        for j in range(len(sizes)):
            if not np.isnan(grid[i, j]):
                ax.text(j, i, f"{grid[i, j]:.2f}", ha="center", va="center", fontsize=8)
    fig.colorbar(im, ax=ax, label=metric)
    ax.set_title(f"Family x size — tools arm  ({metric})")
    fig.tight_layout()
    _save(fig, out_dir, f"C8_family_size_heatmap__{metric}.png")


def write_csv(rows, out_dir):
    path = out_dir / "comparison_summary.csv"
    cols = [
        "key", "slug", "family", "size_b", "quant", "quant_bits", "condition",
        "n_tasks", "failures", "mean_numeric", "mean_judge", "mean_keyed",
        "mean_structured", "mean_overall", "mean_normalized",
    ]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(rows, key=lambda r: (r["condition"], r["family"], r["size_b"] or 0)):
            w.writerow([
                r.get(c) if not isinstance(r.get(c), float) else f"{r.get(c):.4f}"
                for c in cols
            ])
    print(f"  wrote {path}")


def _save(fig, out_dir, name):
    p = out_dir / name
    fig.savefig(p, dpi=130)
    plt.close(fig)
    print(f"  wrote {name}")


def _build_parser():
    p = argparse.ArgumentParser(description="Compare benchmark runs across size/quant/condition")
    p.add_argument("--tools-dir", default="benchmark_results_v2")
    p.add_argument("--notools-dir", default="benchmark_results_v2_notools")
    p.add_argument("--out-dir", default="benchmark_comparison_charts")
    p.add_argument(
        "--metrics",
        nargs="+",
        default=["mean_numeric", "mean_judge"],
        help="metric(s) to chart on (default: mean_numeric mean_judge). Also valid: "
        "mean_keyed, mean_overall, mean_normalized.",
    )
    return p


def main():
    args = _build_parser().parse_args()
    tools_dir = (ROOT / args.tools_dir).resolve()
    notools_dir = (ROOT / args.notools_dir).resolve()
    out_dir = (ROOT / args.out_dir).resolve()
    out_dir.mkdir(exist_ok=True)

    runs = {}
    if tools_dir.exists():
        runs.update(discover(tools_dir, "tools"))
    if notools_dir.exists():
        runs.update(discover(notools_dir, "no_tools"))
    if not runs:
        raise SystemExit(f"No scores_*.jsonl found in {tools_dir} or {notools_dir}")

    rows = summarize(runs)
    print(f"Parsed {len(rows)} run(s). Charts -> {out_dir}/")
    for r in sorted(rows, key=lambda r: (r["condition"], r["family"], r["size_b"] or 0)):
        print(
            f"  {r['key']:34s} fam={r['family']:5s} size={str(r['size_b']):>5s}B "
            f"quant={r['quant']:5s} numeric={_nice(r['mean_numeric'])} "
            f"judge={_nice(r['mean_judge'])} n={r['n_tasks']}"
        )

    for metric in args.metrics:
        chart_score_vs_size(rows, out_dir, metric)
        chart_score_vs_quant(rows, out_dir, metric)
        chart_tools_vs_notools(rows, out_dir, metric)
        chart_leaderboard(rows, out_dir, metric)
        chart_family_means(rows, out_dir, metric)
        chart_quant_bits_scatter(rows, out_dir, metric)
        chart_family_size_heatmap(rows, out_dir, metric)
    # Metric-pair chart (uses numeric AND judge together, so generated once).
    chart_numeric_vs_judge(rows, out_dir)
    write_csv(rows, out_dir)
    print("Done.")


if __name__ == "__main__":
    main()
