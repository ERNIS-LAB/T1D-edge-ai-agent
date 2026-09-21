"""Generate per-experiment figures and LaTeX tables for samplepaper.tex.

The paper's Experiments section maps four RQs onto a handful of model groupings.
This script slices the benchmark results by those groupings and emits, for each
grouping, focused figures (into assets/) and booktabs LaTeX tables (into tables/)
that drop straight into the Results section. See the numbering note below: the
EXP<N> ids here are grouping ids, not the paper's experiment numbers.

    ./venv/bin/python python -m analysis.paper_figures

The reduced-context grouping (EXP4) is included when its edge runs are present
under benchmark_results_v2_edge/.
"""

import ast
import json
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from analysis import compare as cb
from analysis.analyze import (
    is_failure,
    load_task_meta,
    mean as amean,
    metric_score,
    overall_score,
)

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
TABLES = ROOT / "tables"
FAM_C = {"Qwen": "#3f51b5", "Gemma": "#e5732a", "Kimi": "#2e9e5b"}

# When True, a failed (empty-response) task scores 0 on every dimension and 0
# overall, and is kept in the denominator — see strictify()/_task_overall().
STRICT = False

# ---- experiment -> model slugs (tools arm unless noted) --------------------
#
# NOTE ON NUMBERING. The EXP<N> names below, and the tab_exp<N>/exp<N>_* files
# they emit, are internal ids for MODEL GROUPINGS. They no longer match the
# paper's experiment numbers: the paper splits the EXP1 grouping into two
# experiments, the unconstrained ceiling (tools arm) and the value of tools
# (toolless ablation), which shifts everything after it by one:
#
#   EXP1_TOOLS   -> paper Experiment 1   (unconstrained capability)
#   EXP1_NOTOOLS -> paper Experiment 2   (the value of tools)
#   EXP2         -> paper Experiment 3   (small language models)
#   EXP3         -> paper Experiment 4   (quantisation)
#   EXP4         -> paper Experiment 5   (edge constraints)
#                   paper Experiment 6   (serving perf) is measured on-device
#                                        and hand-maintained in tables/tab_hw.tex
#
# Only the caption text carries the paper number; filenames and \label keys keep
# the grouping id, so renumbering the paper again means editing captions only.
#
# Gemma 4-bit is always the QAT build (gemma-*-it-qat); the plain 4-bit MLX
# builds (gemma-4-e2b-4bit) are excluded from the paper.
EXP1_TOOLS = ["kimi", "qwen3-6-35b-a3b", "qwen3-6-27b", "gemma-4-26b-a4b-qat", "gemma-4-31b-qat"]
EXP1_NOTOOLS = ["kimi", "qwen3-6-35b-a3b"]  # models run in BOTH arms
# EXP2 (paper Exp. 3) is the 8-bit arm: every model at Q8, so differences are size and
# family rather than quantisation. The 12B is excluded — it was only ever run as
# a 4-bit QAT build, which would make this a mixed-quant comparison.
EXP2 = ["gemma-4-e4b-8bit", "gemma-4-e2b-8bit", "qwen3-5-9b-q8_0", "qwen3-5-4b-q8_0"]
EXP3_QWEN9B = ["qwen3-5-9b-iq3_xxs", "qwen3-5-9b-q4_0", "qwen3-5-9b-q6_k",
               "qwen3-5-9b-q8_0", "qwen3-5-9b-bf16"]
EXP3_QWEN4B = ["qwen3-5-4b-q4_0", "qwen3-5-4b-q8_0", "qwen3-5-4b-bf16"]
EXP3_GEMMA_E4B = ["gemma-4-e4b-8bit", "gemma-4-e4b-it-qat"]
EXP3_GEMMA_E2B = ["gemma-4-e2b-8bit", "gemma-4-e2b-it-qat"]
# EXP3 (paper Exp. 4) blocks, in table/figure order: (display name, slugs). The per-category
# heatmap is emitted once per family so each figure stays legible at column width.
EXP3_GROUPS_QWEN = [("Qwen 3.5 9B", EXP3_QWEN9B), ("Qwen 3.5 4B", EXP3_QWEN4B)]
EXP3_GROUPS_GEMMA = [("Gemma 4 E4B", EXP3_GEMMA_E4B), ("Gemma 4 E2B", EXP3_GEMMA_E2B)]
EXP3_GROUPS = EXP3_GROUPS_QWEN + EXP3_GROUPS_GEMMA
# EXP4 (paper Exp. 5): same model+quant run at a REDUCED context vs its 32k
# counterpart. Qwen only — the Gemma edge runs are reported in EXP2/EXP3 instead.
EXP4 = ["qwen3-5-9b-iq3_xxs", "qwen3-5-4b-q4_0"]
EXP4_CTX = {"qwen3-5-9b-iq3_xxs": 4096, "qwen3-5-4b-q4_0": 8192}
# The Gemma QAT builds are edge targets too, but they load at the full 32k on the
# device, so they have no reduced-context counterpart to pair against and appear
# in the EXP4 table as single 32k rows.
EXP4_FULL_ONLY = ["gemma-4-e2b-it-qat", "gemma-4-e4b-it-qat"]
# Size of the loaded model WEIGHTS through Ollama, per edge target. This is not
# what benchmark_results_v2_edge/edge_memory.csv records: that file measures the
# whole resident footprint at a given context, so it includes the KV cache and
# grows with the window. The table reports weights, which are a property of the
# build alone, hence one value per model rather than one per context row.
EXP4_WEIGHTS = {
    "qwen3-5-9b-iq3_xxs": "4.0\\,GB",
    "qwen3-5-4b-q4_0": "3.1\\,GB",
    "gemma-4-e2b-it-qat": "1.4\\,GB",
    "gemma-4-e4b-it-qat": "2.8\\,GB",
}
FULL_CTX = 32768


# Every dimension the evaluator can score, as (metric key, row key, column head).
# Tasks are scored on a SUBSET of these — see _task_overall(): Overall is the mean
# over the dimensions a task actually got, averaged over tasks, so it is not the
# average of the per-dimension column means below.
DIMENSIONS = [
    ("tool_correctness", "mean_tool", "Tool"),
    ("argument_correctness", "mean_argument", "Arg."),
    ("numeric_groundedness", "mean_numeric", "Num."),
    ("structured_conclusion", "mean_structured", "Struct."),
    ("keyed_conclusion", "mean_keyed", "Keyed"),
    ("judge", "mean_judge", "Judge"),
    ("logged_state", "mean_logged", "State"),
    ("turn_completion", "mean_turn", "Turn"),
    ("per_turn_state", "mean_pts", "P/T"),
]


# Bar-chart series: every dimension, using the same short heads as the tables.
DIM_BARS = [(key, head) for _, key, head in DIMENSIONS]

# The four criteria the LLM judge scores 0-10 on, which the scorer averages into
# the single `judge` dimension above. Splitting them back out shows WHICH part of
# the judged rubric a model is losing on — see judge_criteria_means().
JUDGE_CRITERIA = [
    ("groundedness", "Groundedness"),
    ("completeness", "Completeness"),
    ("clarity", "Clarity"),
    ("clinical_caution", "Clin. caution"),
]
JUDGE_COLORS = ["#3f51b5", "#e5732a", "#2e9e5b", "#8e5ea2"]
# Used when a judge panel's series are conditions (tools/no-tools, 32k/4k)
# rather than criteria, so the four criterion colours stay free for the bars.
JUDGE_SERIES_COLORS = ["#3f51b5", "#e5732a", "#2e9e5b", "#8e5ea2"]


def _task_overall(rec):
    """Per-task overall score; 0 for failed tasks when STRICT is on."""
    return 0.0 if (STRICT and is_failure(rec)) else overall_score(rec)


# The scorer stores the judge's per-criterion marks in the free-text detail
# rather than as structured fields, and only one shape carries marks it actually
# used: "Judge scores: {'groundedness': 10, ...}" (a Python dict repr), whose
# four values it averages into the task's judge score.
#
# The other shapes must NOT be mined for numbers. When the judge's reply will not
# parse as JSON, or the judge call raises, benchmark_evaluator.judge_response()
# discards the reply and returns a neutral 0.5 (see its `return 0.5, "Judge
# returned non-JSON: ..."` branch), keeping only a 200-char truncation of the
# text. That truncation often still contains readable marks — but they are not
# what the task was scored on, so reading them back would make this table
# contradict the Judge column it is supposed to decompose.
_JUDGE_PREFIX = "Judge scores:"
_JUDGE_DETAIL = re.compile(r"\{.*\}", re.DOTALL)


def judge_criteria(rec):
    """The judge's four 0-10 criterion marks for one task, rescaled to [0, 1].

    Returns None whenever the scorer did not derive the task's judge score from
    marks — not judged, empty response, or a fallback score — so callers can tell
    'no marks behind this score' from 'marked zero'.
    """
    detail = (rec.get("scores", {}).get("judge") or {}).get("detail", "")
    if not detail.startswith(_JUDGE_PREFIX):
        return None
    m = _JUDGE_DETAIL.search(detail)
    if not m:
        return None
    raw = None
    for parse in (ast.literal_eval, json.loads):
        try:
            raw = parse(m.group(0))
            break
        except (ValueError, SyntaxError):
            continue
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, _ in JUDGE_CRITERIA:
        v = raw.get(key)
        if isinstance(v, (int, float)):
            out[key] = max(0.0, min(1.0, float(v) / 10.0))
    return out if len(out) == len(JUDGE_CRITERIA) else None


def judge_criteria_means(recs, strict=None):
    """Mean of each judge criterion, over the same tasks as the Judge dimension.

    This mirrors dimension_means()'s handling of the `judge` metric task for
    task, so the four criterion means average back to exactly the Judge column
    reported beside them. Three cases have to line up:

      * under the strict rule a failed task scores 0 on every criterion and stays
        in the denominator, as it does on every other dimension;
      * a task scored from marks contributes those marks, and reconstruction is
        exact there because the scorer defines the task's judge score as their
        mean, so averaging per-criterion then across criteria is the same as
        averaging per-task;
      * a task scored by fallback instead of from marks (empty response, or a
        reply the scorer could not parse, which it scores a neutral 0.5)
        contributes that fallback to all four criteria, since there is no
        per-criterion breakdown behind it to report.

    Criteria with no data at all come back as None so the table prints '--'
    rather than a misleading 0.00.
    """
    strict = STRICT if strict is None else strict
    vals = {key: [] for key, _ in JUDGE_CRITERIA}
    for rec in recs:
        if strict and is_failure(rec):
            for key in vals:
                vals[key].append(0.0)
            continue
        score = metric_score(rec, "judge")
        if score is None:
            continue  # this task is not judged at all
        marks = judge_criteria(rec)
        for key in vals:
            vals[key].append(marks[key] if marks else score)
    return {key: (amean(v) if v else None) for key, v in vals.items()}


def paired_judge_criteria(runs, slug, cond_a="tools", cond_b="no_tools"):
    """Per-arm judge-criterion means for one model over the tasks BOTH arms ran.

    Same paired-denominator argument as paired_arm_means(): the toolless arm runs
    a strict subset of the suite, so the arms have to be intersected before their
    criterion means can be differenced.
    """
    a = {r["task_id"]: r for r in records_for(runs, slug, cond_a)}
    b = {r["task_id"]: r for r in records_for(runs, slug, cond_b)}
    common = sorted(set(a) & set(b))
    if not common:
        return None
    return {
        "n": len(common),
        "a": judge_criteria_means([a[t] for t in common]),
        "b": judge_criteria_means([b[t] for t in common]),
    }


def dimension_means(row, recs, strict):
    """Attach a mean for every dimension in DIMENSIONS. Under the strict rule a
    failed task scores 0 on each one and stays in the denominator; otherwise a
    task contributes to a dimension only where that dimension was evaluated."""
    row = dict(row)
    for metric, key, _ in DIMENSIONS:
        vals = []
        for r in recs:
            if strict and is_failure(r):
                vals.append(0.0)
                continue
            s = metric_score(r, metric)
            if s is not None:
                vals.append(s)
        row[key] = amean(vals)
    return row


def strictify(row, recs):
    """Recompute a run's aggregate metrics under the strict rule: a failed task
    scores 0 on every dimension and 0 overall, and stays in the denominator."""
    row = dimension_means(row, recs, strict=True)
    row["mean_overall"] = float(
        np.mean([0.0 if is_failure(r) else overall_score(r) for r in recs])
    )
    return row


# Metric key -> the raw score-card dimension it aggregates. Used by the paired
# tools/no-tools comparison, which has to recompute from records rather than
# reuse the per-arm row aggregates.
PAIRED_METRICS = {"mean_numeric": "numeric_groundedness", "mean_judge": "judge"}


def paired_arm_means(runs, slug, cond_a="tools", cond_b="no_tools"):
    """Per-arm means for one model restricted to the tasks BOTH arms ran.

    The two arms do not cover the same suite: the no-tools arm drops every task
    needing a tool it no longer has (logging, memory, food lookup), so it runs 58
    of the 72 tasks. Averaging each arm over its own tasks therefore compares
    different denominators, and the excluded tasks are not neutral — none of them
    carries a reference_query, so in the tools arm each one takes the scorer's
    "no reference query, skip" branch and contributes a free 1.0 to numeric
    groundedness. Intersecting the task ids first makes the delta a like-for-like
    paired comparison.

    Returns {'n': <paired task count>, 'a_<metric>': ..., 'b_<metric>': ...} or
    None when either arm is missing. Honours the module-level STRICT rule.
    """
    a = {r["task_id"]: r for r in records_for(runs, slug, cond_a)}
    b = {r["task_id"]: r for r in records_for(runs, slug, cond_b)}
    common = sorted(set(a) & set(b))
    if not common:
        return None

    def _mean(src, dim):
        vals = []
        for tid in common:
            rec = src[tid]
            if STRICT and is_failure(rec):
                vals.append(0.0)
                continue
            s = metric_score(rec, dim)
            if s is not None:
                vals.append(s)
        return amean(vals)

    out: dict[str, float] = {"n": len(common)}
    for key, dim in PAIRED_METRICS.items():
        out[f"a_{key}"] = _mean(a, dim)
        out[f"b_{key}"] = _mean(b, dim)
    return out


def _drop(tools_val, notools_val):
    """LaTeX cell for the tools->no-tools change, as a drop (positive = score
    fell when tools were removed)."""
    d = tools_val - notools_val
    return f"$-{d:.2f}$" if d >= 0 else f"$+{-d:.2f}$"


def load_rows(strict=False):
    runs = {}
    td, nd = ROOT / "benchmark_results_v2", ROOT / "benchmark_results_v2_notools"
    ed = ROOT / "benchmark_results_v2_edge"
    if td.exists():
        runs.update(cb.discover(td, "tools"))
    if nd.exists():
        runs.update(cb.discover(nd, "no_tools"))
    if ed.exists():
        runs.update(cb.discover(ed, "edge"))  # reduced-context tools runs
    rows = cb.summarize(runs)
    # cb.summarize only computes a handful of dimensions; fill in the rest so
    # both arms expose every entry in DIMENSIONS.
    rows = [(strictify if strict else lambda x, y: dimension_means(x, y, False))(
        r, cb.load_records(runs[r["key"]]["path"])) for r in rows]
    idx = {(r["slug"], r["condition"]): r for r in rows}
    return runs, idx


def records_for(runs, slug, cond="tools"):
    info = runs.get(f"{slug} [{cond}]")
    return cb.load_records(info["path"]) if info else []


def model_gen(slug):
    """Model generation as a display string: qwen3-5 -> '3.5', gemma-4 -> '4'."""
    s = slug.lower()
    m = re.search(r"qwen(\d+)-(\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"gemma-(\d+)", s)
    if m:
        return m.group(1)
    return ""


def model_size(slug, r):
    # Google writes the Gemma effective sizes capitalised: E2B / E4B.
    if "e2b" in slug:
        return "E2B"
    if "e4b" in slug:
        return "E4B"
    if r["size_b"]:
        return f"{int(r['size_b'])}B"
    return ""


# Display names for the quantisations, as the vendors write them. parse_model()
# collapses these to coarse buckets (q3/q4/q8) for the bit-width axis; these are
# the labels shown to a reader.
_QUANT_DISPLAY = [
    (r"iq3_xxs", "IQ3_XXS"),
    (r"q8_0", "Q8_0"),
    (r"q6_k", "Q6_K"),
    (r"q4_0", "Q4_0"),
    (r"bf16", "BF16"),
    (r"8bit", "8-bit"),
    (r"qat", "Q4 QAT"),
    (r"4bit", "4-bit"),
]
# Identifiers that don't encode their quant in the slug. The MoE builds ship as
# 4-bit but the loader key records no finer detail, so they stay a plain "Q4".
_QUANT_DISPLAY_OVERRIDE = {"qwen3-6-27b": "Q4_K_M", "qwen3-6-35b-a3b": "Q4"}


def quant_label(slug, r):
    """The quantisation as a reader-facing string, e.g. 'IQ3_XXS', 'Q4 QAT'."""
    if r["family"] == "Kimi":
        return "API"
    if slug in _QUANT_DISPLAY_OVERRIDE:
        return _QUANT_DISPLAY_OVERRIDE[slug]
    s = slug.lower()
    for pat, lab in _QUANT_DISPLAY:
        if re.search(pat, s):
            return lab
    return "--"


def model_label(slug, r):
    if r["family"] == "Kimi":
        return "Kimi K2.6"
    parts = [r["family"], model_gen(slug), model_size(slug, r)]
    return " ".join(p for p in parts if p)


def label_with_quant(slug, r):
    """Model label including the quantisation, e.g. 'Gemma 4 E4B (Q4 QAT)'."""
    lbl = model_label(slug, r)
    q = quant_label(slug, r)
    return f"{lbl} ({q})" if q not in ("--", "API") else lbl


def params_label(slug, r):
    if r["family"] == "Kimi":
        return "$\\sim$1T (MoE)"
    if "a3b" in slug:
        return "35B (A3B)"
    if "a4b" in slug:
        return "26B (A4B)"
    return model_size(slug, r)


# Task categories come from the benchmark JSON, whose slugs use American
# spelling; the paper is written in British English, so the axis labels are
# respelled here rather than renaming the data keys.
CAT_SPELLING = {
    "hypoglycemia": "hypoglycaemia",
    "glycemic": "glycaemic",
}


def cat_label(cat):
    words = cat.replace("_", " ").split(" ")
    return " ".join(CAT_SPELLING[w] if w in CAT_SPELLING else w for w in words)


def fmt_title(s):
    """Drop a leading 'Exp. N:' prefix and render the rest in sentence case,
    leaving tokens that carry digits or existing capitals (model sizes, quants,
    acronyms like LLM/QAT) untouched."""
    s = re.sub(r"^Exp\.?\s*\d+\s*[:.\-]\s*", "", s)
    words = s.split(" ")
    for i, w in enumerate(words):
        if any(c.isdigit() or c.isupper() for c in w):
            break  # leading token is a model name/acronym — leave it alone
        for j, c in enumerate(w):
            if c.isalpha():
                words[i] = w[:j] + c.upper() + w[j + 1:]
                break
        break
    return " ".join(words)


def dedup_labels(rows):
    """Model labels including the quantisation for every row."""
    return [label_with_quant(r["slug"], r) for r in rows]


def get(idx, slug, cond="tools"):
    return idx.get((slug, cond))


def _fmt(v):
    return "--" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.2f}"


def _save(fig, name):
    ASSETS.mkdir(exist_ok=True)
    p = ASSETS / name
    fig.savefig(p, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  fig  {p.relative_to(ROOT)}")


# ===========================================================================
# Experiment 1 — unconstrained capability + value of tools
# ===========================================================================


def exp1_figures(runs, idx):
    rows = [get(idx, s) for s in EXP1_TOOLS if get(idx, s)]
    rows.sort(key=lambda r: r["mean_overall"])
    # (a) leaderboard
    fig, ax = plt.subplots(figsize=(7, 3.6))
    labels = [label_with_quant(r["slug"], r) for r in rows]
    ax.barh(range(len(rows)), [r["mean_overall"] for r in rows],
            color=[FAM_C[r["family"]] for r in rows])
    for i, r in enumerate(rows):
        ax.text(r["mean_overall"] + 0.005, i, f"{r['mean_overall']:.2f}", va="center", fontsize=8)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlim(0, 1)
    ax.set_xlabel("mean overall score")
    ax.set_title(fmt_title("Exp. 1: unconstrained models, with tools"))
    _save(fig, "exp1_leaderboard.png")

    # (b) tools vs no-tools on numeric + judge, for the two paired models.
    # Both arms are restricted to the tasks they have in common (see
    # paired_arm_means) so the bars share a denominator.
    pairs = [(s, paired_arm_means(runs, s)) for s in EXP1_NOTOOLS]
    pairs = [(s, p) for s, p in pairs if p and get(idx, s)]
    if not pairs:
        return
    n_paired = pairs[0][1]["n"]
    fig, axes = plt.subplots(1, 2, figsize=(9, 4), sharey=True)
    for ax, metric, title in zip(axes, ("mean_numeric", "mean_judge"),
                                 ("Numeric groundedness", "LLM judge")):
        x = np.arange(len(pairs))
        ax.bar(x - 0.2, [p[f"a_{metric}"] for _, p in pairs], 0.4,
               label="tools", color="#3f51b5")
        ax.bar(x + 0.2, [p[f"b_{metric}"] for _, p in pairs], 0.4,
               label="no tools", color="#e5732a")
        ax.set_xticks(x)
        ax.set_xticklabels([label_with_quant(s, get(idx, s)) for s, _ in pairs], fontsize=8)
        ax.set_ylim(0, 1)
        ax.set_title(fmt_title(title))
        ax.legend(fontsize=8)
    axes[0].set_ylabel("score")
    fig.suptitle(fmt_title(
        f"Exp. 1: value of tools (tools vs. no-tools, {n_paired} shared tasks)"))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    _save(fig, "exp1_tools_vs_notools.png")


# --- shared judge-criteria figure builders ---------------------------------
#
# Two layouts, used by every experiment's judge breakdown:
#   _judge_cluster_figure  x = entry (model), one bar per criterion. Reads best
#                          when the entries are unrelated models.
#   _judge_panel_figure    x = criterion, one bar per series member, one panel
#                          per model. Reads best when the series is an ordered
#                          sweep (quantisation, context) within a fixed model,
#                          matching the layout of the other per-sweep figures.


def _judge_cluster_figure(entries, title, fname, rotation=15):
    """entries: [(label, crit_dict)]. One cluster per entry, one bar per criterion."""
    entries = [(lbl, c) for lbl, c in entries if c and any(v is not None for v in c.values())]
    if not entries:
        return
    fig, ax = plt.subplots(figsize=(max(7.0, 1.8 * len(entries)), 4.2))
    x = np.arange(len(entries))
    width = 0.8 / len(JUDGE_CRITERIA)
    for i, ((key, head), color) in enumerate(zip(JUDGE_CRITERIA, JUDGE_COLORS)):
        offs = (i - (len(JUDGE_CRITERIA) - 1) / 2) * width
        ax.bar(x + offs, [c.get(key) or 0.0 for _, c in entries], width,
               label=head, color=color)
    ax.set_xticks(x)
    # The quantisation suffixes make these labels long enough to collide at
    # column width, so they are angled rather than shrunk further.
    ax.set_xticklabels([lbl for lbl, _ in entries], fontsize=8,
                       rotation=rotation, ha="right" if rotation else "center")
    ax.set_ylim(0, 1)
    ax.set_ylabel("mean criterion score")
    ax.set_title(fmt_title(title), pad=34)
    # Above the axes: at these scores every in-axes corner is occupied by bars.
    ax.legend(fontsize=8, ncol=len(JUDGE_CRITERIA), loc="lower center",
              bbox_to_anchor=(0.5, 1.005), frameon=False)
    _save(fig, fname)


def _judge_panel_figure(panels, suptitle, fname, legend_title=None, color=None):
    """panels: [(panel_title, [(series_label, crit_dict)])]. One panel per model.

    When `color` is given the series share that hue and separate by alpha, which
    is how the quantisation figures encode an ordered sweep; otherwise they take
    distinct colours.
    """
    panels = [(t, [(l, c) for l, c in ss if c]) for t, ss in panels]
    panels = [(t, ss) for t, ss in panels if ss]
    if not panels:
        return
    fig, axes = plt.subplots(1, len(panels), figsize=(4.6 * len(panels), 4), sharey=True)
    axes = np.atleast_1d(axes)
    xs = np.arange(len(JUDGE_CRITERIA))
    for ax, (ptitle, series) in zip(axes, panels):
        w = 0.8 / len(series)
        for i, (slabel, crit) in enumerate(series):
            kw = ({"color": color, "alpha": 0.45 + 0.55 * i / max(len(series) - 1, 1)}
                  if color else {"color": JUDGE_SERIES_COLORS[i % len(JUDGE_SERIES_COLORS)]})
            ax.bar(xs + (i - (len(series) - 1) / 2) * w,
                   [crit.get(k) or 0.0 for k, _ in JUDGE_CRITERIA], w,
                   label=slabel, **kw)
        ax.set_xticks(xs)
        ax.set_xticklabels([h for _, h in JUDGE_CRITERIA], fontsize=8, rotation=20,
                           ha="right")
        ax.set_ylim(0, 1)
        ax.set_title(fmt_title(ptitle), fontsize=9)
        ax.legend(fontsize=7, title=legend_title, frameon=False)
    axes[0].set_ylabel("mean criterion score")
    fig.suptitle(fmt_title(suptitle))
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    _save(fig, fname)


def _crit_for(runs, slug, cond="tools"):
    return judge_criteria_means(records_for(runs, slug, cond))


def _judge_sorted_entries(runs, idx, slugs, cond="tools"):
    """(label, criteria) per model, best judge score first."""
    rows = sorted([get(idx, s, cond) for s in slugs if get(idx, s, cond)],
                  key=lambda r: -(r["mean_judge"] or 0))
    return [(label_with_quant(r["slug"], r), _crit_for(runs, r["slug"], cond)) for r in rows]


def exp1_judge_figures(runs, idx):
    """Split the single Judge column into the four rubric criteria it averages.

    The headline judge score hides which part of the rubric a model loses: a
    model that fabricates numbers and one that is accurate but never hedges can
    land on the same mean. Every judge figure is emitted per scoring variant, so
    the strict pass writes its own copies alongside the standard ones.
    """
    _judge_cluster_figure(_judge_sorted_entries(runs, idx, EXP1_TOOLS),
                          "Exp. 1: LLM-judge rubric by criterion",
                          "exp1_judge_criteria.png")

    # The same split applied to the value-of-tools comparison: which parts of the
    # rubric collapse when the model has to answer from parametric memory.
    pairs = [(s, paired_judge_criteria(runs, s)) for s in EXP1_NOTOOLS]
    pairs = [(s, p) for s, p in pairs if p and get(idx, s)]
    if not pairs:
        return
    n_paired = pairs[0][1]["n"]
    _judge_panel_figure(
        [(label_with_quant(s, get(idx, s)), [("tools", p["a"]), ("no tools", p["b"])])
         for s, p in pairs],
        f"Exp. 1: judge criteria with and without tools ({n_paired} shared tasks)",
        "exp1_judge_criteria_tools.png")


def exp2_judge_figure(runs, idx):
    """Paper Exp. 3: judge criteria across the small models."""
    _judge_cluster_figure(_judge_sorted_entries(runs, idx, EXP2),
                          "Exp. 3: LLM-judge rubric by criterion (small models)",
                          "exp2_judge_criteria.png")


def exp3_judge_figures(runs, idx):
    """Paper Exp. 4: judge criteria against quantisation level, per model."""
    for groups, fam, fname in [
        (EXP3_GROUPS_QWEN, "Qwen", "exp3_judge_criteria_qwen.png"),
        (EXP3_GROUPS_GEMMA, "Gemma", "exp3_judge_criteria_gemma.png"),
    ]:
        panels = []
        for name, slugs in groups:
            # Ascending precision, matching _quant_bars() and the per-category
            # heatmaps, so every quantisation figure reads left-to-right in the
            # same direction.
            rows = sorted([get(idx, s) for s in slugs if get(idx, s)],
                          key=lambda r: r["quant_bits"] or 0)
            panels.append((name, [(quant_label(r["slug"], r), _crit_for(runs, r["slug"]))
                                  for r in rows]))
        _judge_panel_figure(panels, f"Exp. 4: judge criteria by quantisation ({fam})",
                            fname, legend_title="quant", color=FAM_C[fam])


def exp4_judge_figure(runs, idx):
    """Paper Exp. 5: judge criteria at full vs. reduced context."""
    panels = []
    for s, full, _ in exp4_pairs(idx):
        panels.append((label_with_quant(s, full), [
            ("32k", _crit_for(runs, s, "tools")),
            (f"{EXP4_CTX[s] // 1024}k", _crit_for(runs, s, "edge")),
        ]))
    _judge_panel_figure(panels, "Exp. 5: judge criteria at full vs. reduced context",
                        "exp4_judge_criteria.png", legend_title="context")


# ===========================================================================
# Experiment 2 — small language models
# ===========================================================================


def exp2_figures(idx):
    rows = [get(idx, s) for s in EXP2 if get(idx, s)]
    rows.sort(key=lambda r: r["mean_overall"])
    ceiling = max(get(idx, s)["mean_overall"] for s in EXP1_TOOLS if get(idx, s))
    # (a) leaderboard vs ceiling
    fig, ax = plt.subplots(figsize=(7, 3.4))
    ax.barh(range(len(rows)), [r["mean_overall"] for r in rows],
            color=[FAM_C[r["family"]] for r in rows])
    for i, r in enumerate(rows):
        ax.text(r["mean_overall"] + 0.005, i, f"{r['mean_overall']:.2f}", va="center", fontsize=8)
    ax.axvline(ceiling, color="grey", ls="--", lw=1.5, label=f"Exp.1 ceiling ({ceiling:.2f})")
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels(dedup_labels(rows), fontsize=9)
    ax.set_xlim(0, 1)
    ax.set_xlabel("mean overall score")
    ax.set_title(fmt_title("Exp. 2: small models vs. unconstrained ceiling"))
    ax.legend(fontsize=8, loc="lower right")
    _save(fig, "exp2_leaderboard.png")

    # (b) metric breakdown
    metrics = DIM_BARS
    x = np.arange(len(metrics))
    w = 0.8 / len(rows)
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    labels = dedup_labels(rows)
    for i, r in enumerate(rows):
        ax.bar(x + i * w, [r[m] for m, _ in metrics], w, label=labels[i],
               color=FAM_C[r["family"]], alpha=0.55 + 0.45 * i / max(len(rows) - 1, 1))
    ax.set_xticks(x + w * (len(rows) - 1) / 2)
    ax.set_xticklabels([lbl for _, lbl in metrics])
    ax.set_ylim(0, 1)
    ax.set_ylabel("score")
    ax.set_title(fmt_title("Exp. 2: metric breakdown across small models"))
    ax.legend(fontsize=7, ncol=2)
    _save(fig, "exp2_metrics.png")


# ===========================================================================
# Experiment 3 — quantisation
# ===========================================================================


EXP3_METRICS = DIM_BARS + [("mean_overall", "Overall")]


def _quant_bars(ax, idx, slugs, name, color):
    """Grouped bars for one model: a group per metric, a bar per quantisation.
    Bars ascend in precision left to right and the alpha ramps with bit-width,
    so the quant ordering reads off the shading as well as the position."""
    rows = sorted([get(idx, s) for s in slugs if get(idx, s)],
                  key=lambda r: r["quant_bits"] or 0)
    if not rows:
        return
    x = np.arange(len(EXP3_METRICS))
    w = 0.8 / len(rows)
    for i, r in enumerate(rows):
        ax.bar(x + (i - (len(rows) - 1) / 2) * w, [r[m] for m, _ in EXP3_METRICS], w,
               label=quant_label(r["slug"], r), color=color,
               alpha=0.45 + 0.55 * i / max(len(rows) - 1, 1))
    ax.set_xticks(x)
    ax.set_xticklabels([lbl for _, lbl in EXP3_METRICS], fontsize=8)
    ax.set_ylim(0, 1)
    ax.set_title(name, fontsize=10)
    # Scores are bounded at 1, so there is no headroom for an in-axes legend. In
    # the stacked layout it goes beside the panel rather than beneath it, which
    # would otherwise open a gap between the two models.
    ax.legend(fontsize=7, title="quant", frameon=False,
              loc="center left", bbox_to_anchor=(1.01, 0.5))


def _quant_bar_figure(idx, groups, suptitle, fname, color):
    # Panels stack vertically, sharing the metric axis, so the two models line up
    # metric-by-metric down the column.
    fig, axes = plt.subplots(len(groups), 1, figsize=(7.8, 3.4 * len(groups)),
                             sharex=True, sharey=True)
    if len(groups) == 1:
        axes = [axes]
    for ax, (name, slugs) in zip(axes, groups):
        _quant_bars(ax, idx, slugs, name, color)
        ax.set_ylabel("score")
    fig.suptitle(fmt_title(suptitle))
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    _save(fig, fname)


def exp3_figures(idx):
    _quant_bar_figure(idx, EXP3_GROUPS_QWEN,
                      "Exp. 3: Qwen quantisation (tools)",
                      "exp3_qwen_quant.png", FAM_C["Qwen"])
    _quant_bar_figure(idx, EXP3_GROUPS_GEMMA,
                      "Exp. 3: Gemma quantisation (8-bit vs. 4-bit QAT)",
                      "exp3_gemma_quant.png", FAM_C["Gemma"])


# ===========================================================================
# Experiment 4 — reduced context window (edge)
# ===========================================================================


def exp4_pairs(idx):
    out = []
    for s in EXP4:
        full, edge = get(idx, s, "tools"), get(idx, s, "edge")
        if full and edge:
            out.append((s, full, edge))
    return out


def exp4_figures(idx):
    pairs = exp4_pairs(idx)
    if not pairs:
        print("  (exp4 skipped — no edge runs yet)")
        return
    metrics = [("mean_overall", "Overall")] + DIM_BARS
    fig, axes = plt.subplots(1, len(pairs), figsize=(4.2 * len(pairs), 4), sharey=True)
    if len(pairs) == 1:
        axes = [axes]
    for ax, (s, full, edge) in zip(axes, pairs):
        x = np.arange(len(metrics))
        ax.bar(x - 0.2, [full[m] for m, _ in metrics], 0.4, label="32k", color="#3f51b5")
        ax.bar(x + 0.2, [edge[m] for m, _ in metrics], 0.4,
               label=f"{EXP4_CTX[s] // 1024}k", color="#e5732a")
        ax.set_xticks(x)
        ax.set_xticklabels([lbl for _, lbl in metrics], fontsize=8)
        ax.set_ylim(0, 1)
        ax.set_title(fmt_title(label_with_quant(s, full)), fontsize=10)
        ax.legend(fontsize=8, title="context")
    axes[0].set_ylabel("score")
    fig.suptitle(fmt_title("Exp. 4: full vs. reduced context window"))
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    _save(fig, "exp4_context.png")


def table_exp4(idx):
    pairs = exp4_pairs(idx)
    full_only = [(s, get(idx, s, "tools")) for s in EXP4_FULL_ONLY]
    full_only = [(s, r) for s, r in full_only if r]
    if not pairs and not full_only:
        return
    lines = [
        "\\begin{table*}[!t]", "\\centering",
        "\\caption{Experiment~5: edge-target models at full (32k) and reduced "
        "context windows. Each reduced window is the maximum Ollama would load for "
        "that model on the device, or the original 32k where it allowed. Footprint "
        "is the size of the loaded model weights through Ollama. Columns otherwise "
        "as in Table~\\ref{tab:exp1}.}",
        "\\label{tab:exp4}", "\\small", "\\setlength{\\tabcolsep}{4pt}",
        "\\begin{tabular}{@{}lll r" + DIM_COLS + "r@{}}", "\\toprule",
        "Model & Context & Footprint & Overall & " + DIM_HEADS + " & Fail \\\\",
        "\\midrule",
    ]
    # Weights are a property of the build, so the footprint sits on the model's
    # first row and the reduced-context row beneath it is left blank.
    for s, full, edge in pairs:
        name = _tex(label_with_quant(s, full))
        foot = EXP4_WEIGHTS.get(s, "--")
        lines.append(f"{name} & 32k & {foot} & {_row_metrics(full)} \\\\")
        lines.append(f" & {EXP4_CTX[s] // 1024}k & --- & {_row_metrics(edge)} \\\\")
        lines.append("\\addlinespace")
    for s, r in full_only:
        name = _tex(label_with_quant(s, r))
        lines.append(f"{name} & 32k & {EXP4_WEIGHTS.get(s, '--')} & {_row_metrics(r)} \\\\")
        lines.append("\\addlinespace")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    _write_table("tab_exp4.tex", "\n".join(lines))


# ===========================================================================
# Per-category heatmaps (category x model, mean even score)
# ===========================================================================


def category_heatmap(runs, idx, slugs, cond, title, fname, label_quant=False, cond_override=None):
    meta = load_task_meta()
    cond_override = cond_override or {}
    cond_for = lambda s: cond_override.get(s, cond)
    recs_by_model = {s: records_for(runs, s, cond_for(s)) for s in slugs}
    recs_by_model = {s: r for s, r in recs_by_model.items() if r}
    models = [s for s in slugs if s in recs_by_model]
    if not models:
        return
    cats = sorted({meta.get(r["task_id"], {}).get("category", "?")
                   for recs in recs_by_model.values() for r in recs})
    grid = np.full((len(cats), len(models)), np.nan)
    for j, s in enumerate(models):
        by_cat = defaultdict(list)
        for r in recs_by_model[s]:
            by_cat[meta.get(r["task_id"], {}).get("category", "?")].append(_task_overall(r))
        for i, c in enumerate(cats):
            if by_cat[c]:
                grid[i, j] = float(np.mean(by_cat[c]))
    # For quant sweeps, disambiguate same-model columns by their quant level;
    # otherwise dedup only where two columns would share a label.
    if label_quant:
        labels = [f"{model_label(s, idx[(s, cond_for(s))])} {idx[(s, cond_for(s))]['quant']}" for s in models]
    else:
        labels = dedup_labels([idx[(s, cond_for(s))] for s in models])
    fig, ax = plt.subplots(figsize=(1.15 * len(models) + 2.5, 0.42 * len(cats) + 1.6))
    im = ax.imshow(grid, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels(labels, rotation=25, ha="right", fontsize=8)
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels([cat_label(c) for c in cats], fontsize=8)
    for i in range(len(cats)):
        for j in range(len(models)):
            if not np.isnan(grid[i, j]):
                ax.text(j, i, f"{grid[i, j]:.2f}", ha="center", va="center", fontsize=7)
    fig.colorbar(im, ax=ax, label="mean score (average over dimensions)")
    ax.set_title(fmt_title(title))
    fig.tight_layout()
    _save(fig, fname)


def _cat_grid(recs_by_slug, cats, slugs, meta):
    """categories x slugs matrix of mean per-task overall score."""
    grid = np.full((len(cats), len(slugs)), np.nan)
    for j, s in enumerate(slugs):
        by_cat = defaultdict(list)
        for r in recs_by_slug[s]:
            by_cat[meta.get(r["task_id"], {}).get("category", "?")].append(_task_overall(r))
        for i, c in enumerate(cats):
            if by_cat[c]:
                grid[i, j] = float(np.mean(by_cat[c]))
    return grid


def category_heatmap_per_model(runs, idx, groups, cond, title, fname):
    """One heatmap panel per model, with that model's quantisations as columns,
    so the comparison a reader makes is within-model rather than across the
    whole sweep. Panels share the category rows and the colour scale."""
    meta = load_task_meta()
    panels = []
    for name, slugs in groups:
        recs = {s: records_for(runs, s, cond) for s in slugs}
        recs = {s: r for s, r in recs.items() if r}
        # Columns ascend in precision, matching the x-axis of the quant line plots.
        present = sorted((s for s in slugs if s in recs),
                         key=lambda s: idx[(s, cond)]["quant_bits"] or 0)
        if present:
            panels.append((name, present, recs))
    if not panels:
        return
    cats = sorted({meta.get(r["task_id"], {}).get("category", "?")
                   for _, present, recs in panels for s in present for r in recs[s]})
    widths = [len(p[1]) for p in panels]
    fig, axes = plt.subplots(
        1, len(panels), sharey=True,
        figsize=(0.95 * sum(widths) + 1.4 * len(panels) + 2.0, 0.42 * len(cats) + 2.0),
        gridspec_kw={"width_ratios": widths},
    )
    if len(panels) == 1:
        axes = [axes]
    mappables = []
    for ax, (name, present, recs) in zip(axes, panels):
        grid = _cat_grid(recs, cats, present, meta)
        im = ax.imshow(grid, aspect="auto", cmap="RdYlGn", vmin=0, vmax=1)
        mappables.append(im)
        ax.set_xticks(range(len(present)))
        ax.set_xticklabels([quant_label(s, idx[(s, cond)]) for s in present],
                           rotation=25, ha="right", fontsize=8)
        ax.set_title(name, fontsize=9)
        for i in range(len(cats)):
            for j in range(len(present)):
                if not np.isnan(grid[i, j]):
                    ax.text(j, i, f"{grid[i, j]:.2f}", ha="center", va="center", fontsize=7)
    axes[0].set_yticks(range(len(cats)))
    axes[0].set_yticklabels([cat_label(c) for c in cats], fontsize=8)
    # Panels share vmin/vmax, so any one mappable drives the shared colourbar.
    fig.colorbar(mappables[0], ax=axes, label="mean score (average over dimensions)",
                 fraction=0.025)
    fig.suptitle(fmt_title(title))
    _save(fig, fname)


# ===========================================================================
# LaTeX tables
# ===========================================================================


def _tex(s):
    """Escape characters that are special in LaTeX text mode. Our labels only
    ever contain underscores (quant names such as Q8_0, IQ3_XXS); matplotlib
    renders those literally, so the escape belongs here rather than in the
    label helpers shared with the figures."""
    return s.replace("_", "\\_")


def _write_table(name, body):
    TABLES.mkdir(exist_ok=True)
    p = TABLES / name
    p.write_text(body, encoding="utf-8")
    print(f"  tab  {p.relative_to(ROOT)}")


DIM_HEADS = " & ".join(head for _, _, head in DIMENSIONS)
DIM_COLS = "r" * len(DIMENSIONS)


def _row_metrics(r):
    # Overall is the mean of the PER-TASK means, so it is not the average of the
    # per-dimension cells that follow it — see the Table 1 caption.
    fr = 100.0 * r["failures"] / r["n_tasks"]
    dims = " & ".join(_fmt(r[key]) for _, key, _ in DIMENSIONS)
    return f"{r['mean_overall']:.2f} & {dims} & {fr:.0f}\\%"


def table_exp1(idx):
    rows = sorted([get(idx, s) for s in EXP1_TOOLS if get(idx, s)],
                  key=lambda r: -r["mean_overall"])
    lines = [
        "\\begin{table*}[!t]", "\\centering",
        "\\caption{Experiment~1: unconstrained models on the tool-enabled suite. "
        "Each task is scored on the subset of dimensions it defines: Tool "
        "(tool correctness), Arg. (argument correctness), Num. (numeric "
        "groundedness), Struct. (structured conclusion), Keyed (key-aligned "
        "conclusion), Judge (LLM-judge score), State (logged state), Turn (turn "
        "completion), P/T (per-turn state). Each column is that dimension's mean "
        "over the tasks where it was evaluated. Overall is computed per task --- "
        "the unweighted mean of the dimensions that task received --- and then "
        "averaged over tasks; because the dimensions are evaluated on differing "
        "numbers of tasks, Overall is not the average of the columns beside it. A "
        "failed task scores 0 on every dimension and remains in the denominator; "
        "Fail is that non-termination rate.}",
        "\\label{tab:exp1}", "\\small", "\\setlength{\\tabcolsep}{4pt}",
        "\\begin{tabular}{@{}ll r" + DIM_COLS + "r@{}}", "\\toprule",
        "Model & Params & Overall & " + DIM_HEADS + " & Fail \\\\", "\\midrule",
    ]
    for r in rows:
        lines.append(f"{model_label(r['slug'], r)} & {params_label(r['slug'], r)} & {_row_metrics(r)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    _write_table("tab_exp1.tex", "\n".join(lines))


def table_exp1_toolless(runs, idx):
    # Both arms restricted to the tasks they share, so the delta is paired.
    rows = [(s, get(idx, s), paired_arm_means(runs, s)) for s in EXP1_NOTOOLS]
    rows = [(s, t, p) for s, t, p in rows if t and p]
    if not rows:
        return
    n_paired = int(rows[0][2]["n"])
    lines = [
        "\\begin{table*}[!t]", "\\centering",
        "\\caption{Experiment~2: value of tools. Numeric-groundedness and LLM-judge "
        "scores with tools vs.\\ without, for the two models run in both arms. "
        "$\\Delta$ is the drop when tools are removed. The toolless arm cannot run "
        "the tasks that need the tools it removes, so both arms are restricted here "
        f"to the {n_paired} tasks they share; the tool-enabled columns are therefore "
        "over those tasks rather than the full suite, and differ slightly from the "
        "corresponding columns of Table~\\ref{tab:exp1}.}",
        "\\label{tab:exp1_toolless}", "\\small", "\\setlength{\\tabcolsep}{4pt}",
        "\\begin{tabular}{@{}lrrrrrr@{}}", "\\toprule",
        "& \\multicolumn{3}{c}{Numeric} & \\multicolumn{3}{c}{Judge} \\\\",
        "\\cmidrule(lr){2-4}\\cmidrule(lr){5-7}",
        "Model & Tools & No-tools & $\\Delta$ & Tools & No-tools & $\\Delta$ \\\\", "\\midrule",
    ]
    for s, t, p in rows:
        tn, nn = p["a_mean_numeric"], p["b_mean_numeric"]
        tj, nj = p["a_mean_judge"], p["b_mean_judge"]
        lines.append(
            f"{model_label(s, t)} & {tn:.2f} & {nn:.2f} & {_drop(tn, nn)} "
            f"& {tj:.2f} & {nj:.2f} & {_drop(tj, nj)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    _write_table("tab_exp1_toolless.tex", "\n".join(lines))


JUDGE_HEADS = " & ".join(h for _, h in JUDGE_CRITERIA)
# Every judge-criteria table shares this note, since the denominator caveat
# applies identically to all of them.
JUDGE_CAPTION_TAIL = (
    "Each criterion is marked 0 to 10 by the judge against the rubric of "
    "Table~\\ref{tab:judge} and rescaled to $[0,1]$ here; Judge is their mean. "
    "Means are over the judged tasks only, so the denominator is smaller than "
    "the full suite.")


def _judge_table(fname, caption, label, lead_heads, body):
    """A Judge + four-criteria table.

    `lead_heads` are the identifying columns (e.g. 'Model & Quant'); `body` is a
    list of either the literal '\\midrule'/'\\addlinespace' or a
    (lead_cells, judge_mean, criteria) triple.
    """
    if not any(isinstance(b, tuple) for b in body):
        return
    n_lead = lead_heads.count("&") + 1
    lines = [
        "\\begin{table}[H]", "\\centering",
        "\\caption{" + caption + " " + JUDGE_CAPTION_TAIL + "}",
        f"\\label{{{label}}}", "\\small", "\\setlength{\\tabcolsep}{5pt}",
        "\\begin{tabular}{@{}" + "l" * n_lead + " r" + "r" * len(JUDGE_CRITERIA) + "@{}}",
        "\\toprule", f"{lead_heads} & Judge & {JUDGE_HEADS} \\\\", "\\midrule",
    ]
    for item in body:
        if isinstance(item, str):
            lines.append(item)
            continue
        lead, judge, crit = item
        cells = " & ".join(_fmt(crit.get(k)) for k, _ in JUDGE_CRITERIA)
        lines.append(f"{lead} & {_fmt(judge)} & {cells} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}", ""]
    _write_table(fname, "\n".join(lines))


def table_exp1_judge(runs, idx):
    """Paper Exp. 1: judge score decomposed into its four rubric criteria."""
    rows = sorted([get(idx, s) for s in EXP1_TOOLS if get(idx, s)],
                  key=lambda r: -r["mean_judge"])
    body = [(_tex(model_label(r["slug"], r)), r["mean_judge"], _crit_for(runs, r["slug"]))
            for r in rows]
    _judge_table(
        "tab_exp1_judge.tex",
        "Experiment~1: the LLM-judge score of Table~\\ref{tab:exp1} decomposed "
        "into the four rubric criteria.",
        "tab:exp1_judge", "Model", body)


def table_exp2_judge(runs, idx):
    """Paper Exp. 3: judge criteria across the small models."""
    rows = sorted([get(idx, s) for s in EXP2 if get(idx, s)],
                  key=lambda r: -r["mean_judge"])
    body = [(_tex(label_with_quant(r["slug"], r)), r["mean_judge"], _crit_for(runs, r["slug"]))
            for r in rows]
    _judge_table(
        "tab_exp2_judge.tex",
        "Experiment~3: the LLM-judge score of Table~\\ref{tab:exp2} decomposed "
        "into the four rubric criteria, for the small models.",
        "tab:exp2_judge", "Model", body)


def table_exp3_judge(runs, idx):
    """Paper Exp. 4: judge criteria against quantisation level."""
    body = []
    for gi, (name, slugs) in enumerate(EXP3_GROUPS):
        rows = sorted([get(idx, s) for s in slugs if get(idx, s)],
                      key=lambda r: -(r["quant_bits"] or 0))
        for j, r in enumerate(rows):
            lead = f"{name if j == 0 else ''} & {_tex(quant_label(r['slug'], r))}"
            body.append((lead, r["mean_judge"], _crit_for(runs, r["slug"])))
        if gi < len(EXP3_GROUPS) - 1:
            body.append("\\midrule")
    _judge_table(
        "tab_exp3_judge.tex",
        "Experiment~4: the LLM-judge score of Table~\\ref{tab:exp3} decomposed "
        "into the four rubric criteria, at each quantisation level.",
        "tab:exp3_judge", "Model & Quant", body)


def table_exp4_judge(runs, idx):
    """Paper Exp. 5: judge criteria at full vs. reduced context."""
    body = []
    pairs = exp4_pairs(idx)
    for s, full, edge in pairs:
        name = _tex(label_with_quant(s, full))
        body.append((f"{name} & 32k", full["mean_judge"], _crit_for(runs, s, "tools")))
        body.append((f" & {EXP4_CTX[s] // 1024}k", edge["mean_judge"],
                     _crit_for(runs, s, "edge")))
        body.append("\\addlinespace")
    # The Gemma QAT builds load at 32k on the device, so they contribute a single
    # row each rather than a full/reduced pair — as in Table~\ref{tab:exp4}.
    for s in EXP4_FULL_ONLY:
        r = get(idx, s)
        if r:
            body.append((f"{_tex(label_with_quant(s, r))} & 32k", r["mean_judge"],
                         _crit_for(runs, s)))
            body.append("\\addlinespace")
    _judge_table(
        "tab_exp4_judge.tex",
        "Experiment~5: the LLM-judge score of Table~\\ref{tab:exp4} decomposed "
        "into the four rubric criteria, at full and reduced context.",
        "tab:exp4_judge", "Model & Context", body)


def table_exp1_judge_toolless(runs, idx):
    """Experiment 1, per-criterion judge scores with tools vs. without."""
    rows = [(s, get(idx, s), paired_judge_criteria(runs, s)) for s in EXP1_NOTOOLS]
    rows = [(s, t, p) for s, t, p in rows if t and p]
    if not rows:
        return
    n_paired = int(rows[0][2]["n"])
    lines = [
        "\\begin{table}[H]", "\\centering",
        "\\caption{Experiment~2: judge criteria with tools vs.\\ without, for the "
        "two models run in both arms. $\\Delta$ is the drop when tools are "
        f"removed. Both arms are restricted to the {n_paired} tasks they share, as "
        "in Table~\\ref{tab:exp1_toolless}.}",
        "\\label{tab:exp1_judge_toolless}", "\\small",
        "\\setlength{\\tabcolsep}{5pt}",
        "\\begin{tabular}{@{}llrrr@{}}", "\\toprule",
        "Model & Criterion & Tools & No-tools & $\\Delta$ \\\\", "\\midrule",
    ]
    for n, (slug, row, p) in enumerate(rows):
        if n:
            lines.append("\\addlinespace")
        for i, (key, head) in enumerate(JUDGE_CRITERIA):
            a, b = p["a"].get(key), p["b"].get(key)
            name = model_label(slug, row) if i == 0 else ""
            delta = _drop(a, b) if a is not None and b is not None else "--"
            lines.append(f"{name} & {head} & {_fmt(a)} & {_fmt(b)} & {delta} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}", ""]
    _write_table("tab_exp1_judge_toolless.tex", "\n".join(lines))


def table_exp2(idx):
    rows = sorted([get(idx, s) for s in EXP2 if get(idx, s)], key=lambda r: -r["mean_overall"])
    labels = dedup_labels(rows)
    lines = [
        "\\begin{table*}[!t]", "\\centering",
        "\\caption{Experiment~3: small language models on the tool-enabled suite, "
        "all at 8-bit so that the comparison isolates model size and family from "
        "quantisation. Columns as in Table~\\ref{tab:exp1}.}",
        "\\label{tab:exp2}", "\\small", "\\setlength{\\tabcolsep}{4pt}",
        "\\begin{tabular}{@{}ll r" + DIM_COLS + "r@{}}", "\\toprule",
        "Model & Params & Overall & " + DIM_HEADS + " & Fail \\\\", "\\midrule",
    ]
    for lbl, r in zip(labels, rows):
        lines.append(f"{_tex(lbl)} & {params_label(r['slug'], r)} & {_row_metrics(r)} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    _write_table("tab_exp2.tex", "\n".join(lines))


def table_exp3(idx):
    groups = EXP3_GROUPS
    lines = [
        "\\begin{table*}[!t]", "\\centering",
        "\\caption{Experiment~4: effect of quantisation. Each block holds one model "
        "at several quantisation levels. Columns as in Table~\\ref{tab:exp1}.}",
        "\\label{tab:exp3}", "\\small", "\\setlength{\\tabcolsep}{4pt}",
        "\\begin{tabular}{@{}ll r" + DIM_COLS + "r@{}}", "\\toprule",
        "Model & Quant & Overall & " + DIM_HEADS + " & Fail \\\\", "\\midrule",
    ]
    for gi, (name, slugs) in enumerate(groups):
        rows = sorted([get(idx, s) for s in slugs if get(idx, s)],
                      key=lambda r: -(r["quant_bits"] or 0))
        for j, r in enumerate(rows):
            mlabel = name if j == 0 else ""
            lines.append(f"{mlabel} & {_tex(quant_label(r['slug'], r))} & {_row_metrics(r)} \\\\")
        if gi < len(groups) - 1:
            lines.append("\\midrule")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table*}", ""]
    _write_table("tab_exp3.tex", "\n".join(lines))


def emit(runs, idx):
    exp1_figures(runs, idx)
    exp1_judge_figures(runs, idx)
    exp2_figures(idx)
    exp2_judge_figure(runs, idx)
    exp3_figures(idx)
    exp3_judge_figures(runs, idx)
    exp4_figures(idx)
    exp4_judge_figure(runs, idx)
    # per-category heatmaps
    category_heatmap(runs, idx, EXP1_TOOLS, "tools",
                     "Exp. 1: per-category score (unconstrained models)",
                     "exp1_category_heatmap.png")
    category_heatmap(runs, idx, EXP2, "tools",
                     "Exp. 2: per-category score (small models)",
                     "exp2_category_heatmap.png")
    category_heatmap_per_model(runs, idx, EXP3_GROUPS_QWEN, "tools",
                               "Exp. 3: per-category score by quantisation (Qwen)",
                               "exp3_category_heatmap_qwen.png")
    category_heatmap_per_model(runs, idx, EXP3_GROUPS_GEMMA, "tools",
                               "Exp. 3: per-category score by quantisation (Gemma)",
                               "exp3_category_heatmap_gemma.png")
    category_heatmap(runs, idx, EXP4, "edge",
                     "Exp. 4: per-category score at reduced context",
                     "exp4_category_heatmap.png")
    table_exp1(idx)
    table_exp1_toolless(runs, idx)
    table_exp1_judge(runs, idx)
    table_exp1_judge_toolless(runs, idx)
    table_exp2(idx)
    table_exp2_judge(runs, idx)
    table_exp3(idx)
    table_exp3_judge(runs, idx)
    table_exp4(idx)
    table_exp4_judge(runs, idx)


def main():
    global ASSETS, TABLES, STRICT

    # Standard scoring -> assets/, tables/
    runs, idx = load_rows()
    print(f"Loaded {len(idx)} run(s). Figures -> assets/, tables -> tables/")
    emit(runs, idx)

    # Strict variant: failed tasks score 0 on every dimension, averaged over all
    # tasks -> assets_strict/, tables_strict/
    STRICT = True
    ASSETS, TABLES = ROOT / "assets_strict", ROOT / "tables_strict"
    runs_s, idx_s = load_rows(strict=True)
    print("Strict (failures scored 0) -> assets_strict/, tables_strict/")
    emit(runs_s, idx_s)

    print("Done.")


if __name__ == "__main__":
    main()
