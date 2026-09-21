from __future__ import annotations

import argparse
import json
import sqlite3
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MG_DL_TO_MMOL_L = 18.0
DEFAULT_DB_PATH = "experiment_data.sqlite3"
DEFAULT_XML_PATH = "data/OhioT1DM/2020/test/540-ws-testing.xml"


def parse_xml_timestamp(timestamp_text: str) -> str:
    """Convert OhioT1DM timestamp (DD-MM-YYYY HH:MM:SS) to UTC ISO 8601."""
    dt = datetime.strptime(timestamp_text, "%d-%m-%Y %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _event_json(event: ET.Element) -> str:
    return json.dumps(dict(event.attrib), sort_keys=True)


def _recreate_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    schema = """
    CREATE TABLE cgm_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        factory_timestamp_utc TEXT NOT NULL,
        device_timestamp_local TEXT,
        glucose_mmol_l REAL NOT NULL,
        trend INTEGER,
        source TEXT DEFAULT 'ohio_t1dm',
        raw_json TEXT,
        inserted_at TEXT,
        UNIQUE(patient_id, factory_timestamp_utc, glucose_mmol_l)
    );
    CREATE INDEX idx_cgm_patient_ts ON cgm_readings(patient_id, factory_timestamp_utc);
    CREATE INDEX idx_cgm_ts ON cgm_readings(factory_timestamp_utc);

    CREATE TABLE cgm_sync_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE patient_metadata (
        patient_id TEXT PRIMARY KEY,
        weight REAL,
        insulin_type TEXT
    );

    CREATE TABLE finger_stick (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        glucose_mg_dl REAL NOT NULL,
        glucose_mmol_l REAL NOT NULL
    );

    CREATE TABLE basal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        rate_units_per_hr REAL NOT NULL
    );

    CREATE TABLE temp_basal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        ts_begin_utc TEXT NOT NULL,
        ts_end_utc TEXT NOT NULL,
        rate_units_per_hr REAL NOT NULL
    );

    CREATE TABLE bolus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        ts_begin_utc TEXT NOT NULL,
        ts_end_utc TEXT NOT NULL,
        bolus_type TEXT NOT NULL,
        dose_units REAL NOT NULL
    );

    CREATE TABLE meal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patient_id TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        meal_type TEXT,
        carbs_g REAL NOT NULL
    );
    """
    conn.executescript(schema)


def _collect_rows(root: ET.Element) -> dict[str, list[tuple[Any, ...]]]:
    patient_id = str(root.attrib.get("id", "unknown")).strip() or "unknown"
    inserted_at = datetime.now(timezone.utc).isoformat()

    glucose_rows: list[tuple[Any, ...]] = []
    finger_rows: list[tuple[Any, ...]] = []
    basal_rows: list[tuple[Any, ...]] = []
    temp_basal_rows: list[tuple[Any, ...]] = []
    bolus_rows: list[tuple[Any, ...]] = []
    meal_rows: list[tuple[Any, ...]] = []

    glucose_node = root.find("glucose_level")
    if glucose_node is not None:
        for event in glucose_node.findall("event"):
            ts = event.attrib.get("ts")
            value_mg_dl = _as_float(event.attrib.get("value"))
            if ts is None or value_mg_dl is None:
                continue
            iso = parse_xml_timestamp(ts)
            glucose_rows.append(
                (
                    patient_id,
                    iso,
                    None,
                    value_mg_dl / MG_DL_TO_MMOL_L,
                    None,
                    "ohio_t1dm",
                    _event_json(event),
                    inserted_at,
                )
            )

    finger_node = root.find("finger_stick")
    if finger_node is not None:
        for event in finger_node.findall("event"):
            ts = event.attrib.get("ts")
            value_mg_dl = _as_float(event.attrib.get("value"))
            if ts is None or value_mg_dl is None:
                continue
            if value_mg_dl == 0.0:
                continue
            iso = parse_xml_timestamp(ts)
            finger_rows.append(
                (
                    patient_id,
                    iso,
                    value_mg_dl,
                    value_mg_dl / MG_DL_TO_MMOL_L,
                )
            )

    basal_node = root.find("basal")
    if basal_node is not None:
        for event in basal_node.findall("event"):
            ts = event.attrib.get("ts")
            value = _as_float(event.attrib.get("value"))
            if ts is None or value is None:
                continue
            basal_rows.append((patient_id, parse_xml_timestamp(ts), value))

    temp_basal_node = root.find("temp_basal")
    if temp_basal_node is not None:
        for event in temp_basal_node.findall("event"):
            begin = event.attrib.get("ts_begin")
            end = event.attrib.get("ts_end")
            value = _as_float(event.attrib.get("value"))
            if begin is None or end is None or value is None:
                continue
            temp_basal_rows.append(
                (
                    patient_id,
                    parse_xml_timestamp(begin),
                    parse_xml_timestamp(end),
                    value,
                )
            )

    bolus_node = root.find("bolus")
    if bolus_node is not None:
        for event in bolus_node.findall("event"):
            begin = event.attrib.get("ts_begin")
            end = event.attrib.get("ts_end")
            bolus_type = event.attrib.get("type")
            dose = _as_float(event.attrib.get("dose"))
            if begin is None or end is None or bolus_type is None or dose is None:
                continue
            bolus_rows.append(
                (
                    patient_id,
                    parse_xml_timestamp(begin),
                    parse_xml_timestamp(end),
                    bolus_type,
                    dose,
                )
            )

    meal_node = root.find("meal")
    if meal_node is not None:
        for event in meal_node.findall("event"):
            ts = event.attrib.get("ts")
            meal_type = event.attrib.get("type")
            carbs = _as_float(event.attrib.get("carbs"))
            if ts is None or carbs is None:
                continue
            meal_rows.append((patient_id, parse_xml_timestamp(ts), meal_type, carbs))

    return {
        "patient_id": [(patient_id,)],
        "cgm_readings": glucose_rows,
        "finger_stick": finger_rows,
        "basal": basal_rows,
        "temp_basal": temp_basal_rows,
        "bolus": bolus_rows,
        "meal": meal_rows,
    }


def _insert_rows(conn: sqlite3.Connection, root: ET.Element, rows: dict[str, list[tuple[Any, ...]]]) -> None:
    patient_id = rows["patient_id"][0][0]
    weight = _as_float(root.attrib.get("weight"))
    insulin_type = root.attrib.get("insulin_type")

    conn.execute(
        "INSERT INTO patient_metadata(patient_id, weight, insulin_type) VALUES (?, ?, ?)",
        (patient_id, weight, insulin_type),
    )
    conn.execute(
        "INSERT INTO cgm_sync_state(key, value) VALUES (?, ?)",
        ("active_patient_id", patient_id),
    )

    conn.executemany(
        """
        INSERT INTO cgm_readings(
            patient_id,
            factory_timestamp_utc,
            device_timestamp_local,
            glucose_mmol_l,
            trend,
            source,
            raw_json,
            inserted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows["cgm_readings"],
    )

    conn.executemany(
        "INSERT INTO finger_stick(patient_id, timestamp_utc, glucose_mg_dl, glucose_mmol_l) VALUES (?, ?, ?, ?)",
        rows["finger_stick"],
    )

    conn.executemany(
        "INSERT INTO basal(patient_id, timestamp_utc, rate_units_per_hr) VALUES (?, ?, ?)",
        rows["basal"],
    )

    conn.executemany(
        "INSERT INTO temp_basal(patient_id, ts_begin_utc, ts_end_utc, rate_units_per_hr) VALUES (?, ?, ?, ?)",
        rows["temp_basal"],
    )

    conn.executemany(
        "INSERT INTO bolus(patient_id, ts_begin_utc, ts_end_utc, bolus_type, dose_units) VALUES (?, ?, ?, ?, ?)",
        rows["bolus"],
    )

    conn.executemany(
        "INSERT INTO meal(patient_id, timestamp_utc, meal_type, carbs_g) VALUES (?, ?, ?, ?)",
        rows["meal"],
    )


def load_ohio_t1dm_xml(xml_path: str, db_path: str = DEFAULT_DB_PATH) -> dict[str, int]:
    """Parse OhioT1DM XML and load data into an experiment SQLite database."""
    xml_file = Path(xml_path)
    if not xml_file.exists():
        raise FileNotFoundError(f"XML file not found: {xml_file}")

    root = ET.parse(xml_file).getroot()

    db_file = Path(db_path)
    conn = _recreate_db(db_file)
    try:
        _create_schema(conn)
        rows = _collect_rows(root)
        _insert_rows(conn, root, rows)
        conn.commit()

        counts: dict[str, int] = {}
        for table in [
            "cgm_readings",
            "finger_stick",
            "basal",
            "temp_basal",
            "bolus",
            "meal",
            "patient_metadata",
            "cgm_sync_state",
        ]:
            result = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
            counts[table] = int(result["c"] if result else 0)
        return counts
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Load OhioT1DM XML into a SQLite experiment DB")
    parser.add_argument("xml_path", nargs="?", default=DEFAULT_XML_PATH, help="Path to OhioT1DM XML file")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="Output SQLite database path")
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    counts = load_ohio_t1dm_xml(args.xml_path, db_path=args.db_path)

    print(f"Loaded OhioT1DM data from {args.xml_path}")
    print(f"SQLite DB: {args.db_path}")
    for table in [
        "cgm_readings",
        "finger_stick",
        "basal",
        "temp_basal",
        "bolus",
        "meal",
        "patient_metadata",
        "cgm_sync_state",
    ]:
        print(f"  {table}: {counts.get(table, 0)}")


if __name__ == "__main__":
    main()
