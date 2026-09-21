#!/usr/bin/env python3
"""
Compute insulin-to-carb ratios per meal type from the OhioT1DM dataset.

Parses all patient XML files, matches each meal to the nearest bolus
within a configurable time window, and computes the average
grams-of-carbs-per-unit-of-insulin ratio for breakfast, lunch, and dinner.

Output is written to a JSON file keyed by patient_id, suitable for use
in benchmarks or by the set_insulin_ratio / calculate_insulin_dosage tools.

Usage:
    python python -m analysis.compute_patient_ratios
    python python -m analysis.compute_patient_ratios --window 30 --output results/patient_ratios.json
    python python -m analysis.compute_patient_ratios --dataset-dir data/OhioT1DM/2020
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DATASET_DIR = "data/OhioT1DM"
DEFAULT_OUTPUT = "results/patient_insulin_ratios.json"
DEFAULT_WINDOW_MINUTES = 30

# Only these meal types are tracked for ratio calculation.
# The OhioT1DM XML uses the exact casing: Breakfast, Lunch, Dinner, Snack.
MEAL_TYPES_OF_INTEREST = {"Breakfast", "Lunch", "Dinner"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_xml_timestamp(timestamp_text: str) -> datetime:
    """Convert OhioT1DM timestamp (DD-MM-YYYY HH:MM:SS) to UTC datetime."""
    dt = datetime.strptime(timestamp_text, "%d-%m-%Y %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def find_xml_files(dataset_dir: str) -> list[Path]:
    """Return all .xml files recursively under *dataset_dir*."""
    root = Path(dataset_dir)
    if not root.is_dir():
        raise NotADirectoryError(f"Dataset directory not found: {root}")
    return sorted(root.rglob("*.xml"))


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def compute_ratios_for_patient(
    xml_paths: Path | list[Path],
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict[str, dict[str, Any]]:
    """
    Parse patient XML(s) and return per-meal-type insulin-to-carb ratios.

    Accepts a single Path or a list of Paths. When multiple files are given
    (e.g. train + test for the same patient), meals and boluses from all files
    are pooled together before matching, yielding a more robust estimate.

    Returns a dict like:
        {
            "breakfast": {"ratio": 15.2, "pairs": 8, "carbs_g": [...], "doses_u": [...]},
            ...
        }

    *ratio* is grams of carbs per unit of insulin (higher = more insulin sensitive).
    Each meal is matched to the nearest bolus within *window_minutes*.
    """
    if isinstance(xml_paths, Path):
        xml_paths = [xml_paths]

    # Pool all meals and boluses across files
    meals: list[dict[str, Any]] = []
    boluses: list[dict[str, Any]] = []

    for xml_path in xml_paths:
        tree = ET.parse(xml_path)
        root = tree.getroot()

        # Collect meals from this file
        meal_node = root.find("meal")
        if meal_node is not None:
            for event in meal_node.findall("event"):
                ts = event.attrib.get("ts")
                meal_type = event.attrib.get("type", "")
                carbs_text = event.attrib.get("carbs")
                if not ts or not carbs_text:
                    continue
                try:
                    carbs = float(carbs_text)
                except ValueError:
                    continue
                if carbs <= 0:
                    continue
                meals.append(
                    {
                        "ts": parse_xml_timestamp(ts),
                        "meal_type": meal_type,
                        "carbs_g": carbs,
                    }
                )

        # Collect boluses from this file (all types)
        bolus_node = root.find("bolus")
        if bolus_node is not None:
            for event in bolus_node.findall("event"):
                begin = event.attrib.get("ts_begin")
                dose_text = event.attrib.get("dose")
                if not begin or not dose_text:
                    continue
                try:
                    dose = float(dose_text)
                except ValueError:
                    continue
                if dose <= 0:
                    continue
                boluses.append(
                    {
                        "ts": parse_xml_timestamp(begin),
                        "dose_u": dose,
                    }
                )

    if not meals or not boluses:
        return {}

    window_seconds = window_minutes * 60
    # Bucket per meal type: list of (carbs_g, matched_dose_u)
    pairs: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for meal in meals:
        meal_type = meal["meal_type"]
        if meal_type not in MEAL_TYPES_OF_INTEREST:
            continue

        # Find nearest bolus within the window
        best_dose: float | None = None
        best_delta: float = float("inf")

        for bolus in boluses:
            delta = abs((bolus["ts"] - meal["ts"]).total_seconds())
            if delta <= window_seconds and delta < best_delta:
                best_dose = bolus["dose_u"]
                best_delta = delta

        if best_dose is not None and best_dose > 0:
            pairs[meal_type].append((meal["carbs_g"], best_dose))

    # Compute ratios
    result: dict[str, dict[str, Any]] = {}
    for meal_type, data in pairs.items():
        carbs_list = [c for c, _ in data]
        doses_list = [d for _, d in data]
        # Ratio = carbs per unit. Average of individual ratios.
        individual_ratios = [c / d for c, d in data]
        avg_ratio = (
            sum(individual_ratios) / len(individual_ratios)
            if individual_ratios
            else 0.0
        )
        result[meal_type.lower()] = {
            "ratio": round(avg_ratio, 1),
            "pairs": len(data),
            "carbs_g": [round(c, 1) for c in carbs_list],
            "doses_u": [round(d, 2) for d in doses_list],
        }

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute insulin-to-carb ratios per patient from OhioT1DM XML files."
    )
    parser.add_argument(
        "--dataset-dir",
        default=DEFAULT_DATASET_DIR,
        help=f"Root directory containing OhioT1DM XML files (default: {DEFAULT_DATASET_DIR})",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON file path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=DEFAULT_WINDOW_MINUTES,
        help=f"Matching window in minutes around each meal (default: {DEFAULT_WINDOW_MINUTES})",
    )
    args = parser.parse_args()

    xml_files = find_xml_files(args.dataset_dir)
    if not xml_files:
        print(f"No XML files found under {args.dataset_dir}")
        return

    # Group files by patient ID so train+test data are combined for a
    # single, more robust estimate per patient.
    by_patient: dict[str, list[Path]] = defaultdict(list)
    for xml_path in xml_files:
        patient_id = xml_path.stem.split("-")[0]  # e.g. "540" from "540-ws-testing.xml"
        by_patient[patient_id].append(xml_path)

    print(f"Found {len(xml_files)} XML file(s) across {len(by_patient)} patient(s).\n")

    all_ratios: dict[str, Any] = {}
    summary_lines: list[str] = []

    for patient_id in sorted(by_patient.keys()):
        # Combine meals and boluses from all files for this patient, then
        # compute ratios from the combined pool.
        combined_ratios = compute_ratios_for_patient(
            by_patient[patient_id], window_minutes=args.window
        )

        if combined_ratios:
            all_ratios[patient_id] = combined_ratios

        # Build per-patient summary line
        parts = [f"{patient_id}:"]
        for mt in ["breakfast", "lunch", "dinner"]:
            if mt in combined_ratios:
                info = combined_ratios[mt]
                parts.append(f"  {mt} 1:{info['ratio']:.1f} ({info['pairs']} pairs)")
            else:
                parts.append(f"  {mt} —")
        summary_lines.append(" ".join(parts))

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(all_ratios, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("Per-patient insulin-to-carb ratios (g carbs / 1 U rapid insulin):\n")
    for line in summary_lines:
        print(line)

    print(f"\nWrote {len(all_ratios)} patient(s) to {output_path}")


if __name__ == "__main__":
    main()
