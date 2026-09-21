"""Merge a partial re-run (a subset of task IDs) back into a model's full result
files, replacing the old records for those tasks.

Used after re-running just the tasks that errored on a flaky remote API: the new
records overwrite the old (errored) ones by task_id, and a fresh combined file is
written with a new timestamp so analyze_/compare_benchmarks pick it up as the latest.

    ./venv/bin/python python -m analysis.merge_rerun \
        --orig-dir benchmark_results_v2_notools/kimi-agent \
        --rerun-dir /tmp/kimi_rerun --slug kimi
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_RE = re.compile(r"^(?P<kind>scores|traces)_(?P<slug>.+)_(?P<ts>\d{8}T\d{6})\.jsonl$")


def latest(dir_path: Path, kind: str, slug: str) -> Path | None:
    best: tuple[str, Path] | None = None
    for p in dir_path.glob(f"{kind}_*.jsonl"):
        m = _RE.match(p.name)
        if m and m.group("slug") == slug:
            ts = m.group("ts")
            if best is None or ts > best[0]:
                best = (ts, p)
    return best[1] if best else None


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def merge_kind(orig_dir: Path, rerun_dir: Path, slug: str, kind: str, new_ts: str):
    orig_path = latest(orig_dir, kind, slug)
    rerun_path = latest(rerun_dir, kind, slug)
    if orig_path is None or rerun_path is None:
        print(f"  [{kind}] skip (orig={orig_path}, rerun={rerun_path})")
        return
    orig = load(orig_path)
    rerun = {r["task_id"]: r for r in load(rerun_path)}
    merged, replaced = [], 0
    for rec in orig:
        if rec["task_id"] in rerun:
            merged.append(rerun[rec["task_id"]])
            replaced += 1
        else:
            merged.append(rec)
    out = orig_dir / f"{kind}_{slug}_{new_ts}.jsonl"
    out.write_text("\n".join(json.dumps(r) for r in merged) + "\n", encoding="utf-8")
    print(f"  [{kind}] replaced {replaced}/{len(rerun)} -> {out.name} ({len(merged)} records)")


def main():
    p = argparse.ArgumentParser(description="Merge a re-run subset into full results")
    p.add_argument("--orig-dir", required=True)
    p.add_argument("--rerun-dir", required=True)
    p.add_argument("--slug", required=True)
    args = p.parse_args()

    orig_dir = Path(args.orig_dir)
    rerun_dir = Path(args.rerun_dir)
    new_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    print(f"Merging {args.slug}: {rerun_dir} -> {orig_dir} (new ts {new_ts})")
    for kind in ("scores", "traces"):
        merge_kind(orig_dir, rerun_dir, args.slug, kind, new_ts)
    print("Done.")


if __name__ == "__main__":
    main()
