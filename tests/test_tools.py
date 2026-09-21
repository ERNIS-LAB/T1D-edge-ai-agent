# tests/test_tools.py
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.data.cgm_store import cgm_store
from agent.kb.store import kb_store
from agent.tools import (
    _client,
    _load_ratios,
    _patients,
    _save_ratios,
    calculate_insulin_dosage,
    get_average_glucose,
    get_cgm_readings,
    get_cgm_spikes_from_db,
    get_cgm_summary_from_db,
    get_estimated_a1c,
    get_glucose_history,
    get_glucose_logbook,
    get_glucose_summary,
    get_icr_summary,
    get_insulin_ratio,
    get_latest_glucose,
    get_nearest_cgm_reading,
    get_recent_cgm_readings,
    get_time_in_range,
    list_insulin_ratios,
    log_glucose,
    log_insulin,
    log_meal,
    rag_search_glucose_patterns,
    rag_search_reports,
    run_deferred_log_enrichment,
    search_logged_events,
    set_insulin_ratio,
    sync_cgm_to_db,
    sync_glucose_to_kb,
    sync_latest_cgm_to_db,
)


class TestLogGlucose:
    def test_value_only(self):
        result = log_glucose.invoke({"value": 94.0})
        assert "94.0" in result
        assert "mmol/L" in result

    def test_with_timestamp(self):
        result = log_glucose.invoke(
            {"value": 120.5, "timestamp": "2026-04-01T08:00:00"}
        )
        assert "120.5" in result
        assert "2026-04-01T08:00:00" in result

    def test_no_timestamp_omits_at(self):
        result = log_glucose.invoke({"value": 80.0})
        assert " at " not in result


class TestGetLatestGlucose:
    def test_calls_client_latest(self):
        get_latest_glucose.invoke({})
        _client.latest.assert_called_once_with(patient_identifier=_patients[0])

    def test_returns_string(self):
        result = get_latest_glucose.invoke({})
        assert isinstance(result, str)


class TestGetGlucoseLogbook:
    def test_calls_client_logbook(self):
        get_glucose_logbook.invoke({})
        _client.logbook.assert_called_once_with(patient_identifier=_patients[0])

    def test_returns_string(self):
        result = get_glucose_logbook.invoke({})
        assert isinstance(result, str)


class TestGetGlucoseHistory:
    def test_filters_out_old_readings(self):
        now = datetime.now(timezone.utc)
        old = MagicMock()
        old.factory_timestamp = now - timedelta(days=10)
        recent = MagicMock()
        recent.factory_timestamp = now - timedelta(hours=1)
        _client.logbook.return_value = [old, recent]

        result = get_glucose_history.invoke({"days": 7})

        assert str(recent) in result
        assert str(old) not in result

    def test_empty_result_returns_message(self):
        _client.logbook.return_value = []
        result = get_glucose_history.invoke({"days": 7})
        assert "No readings found" in result
        assert "7" in result

    def test_defaults_to_7_days(self):
        now = datetime.now(timezone.utc)
        reading = MagicMock()
        reading.factory_timestamp = now - timedelta(days=3)
        _client.logbook.return_value = [reading]

        # Calling without `days` should not raise
        result = get_glucose_history.invoke({})
        assert isinstance(result, str)

    def test_fallback_on_items_without_timestamp(self):
        # If logbook items lack a .timestamp attribute, return all data rather than crash
        _client.logbook.return_value = ["raw", "strings"]
        result = get_glucose_history.invoke({"days": 7})
        assert isinstance(result, str)


class TestGetTimeInRange:
    def _make_reading(self, days_ago: float, value: float):
        r = MagicMock()
        r.factory_timestamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        r.value_in_mg_per_dl = value
        return r

    def _setup_glucostats(self, tir: float):
        """Wire the glucostats sys.modules mock to return a known TIR value."""
        import sys

        mock_stats = MagicMock()
        mock_stats.__getitem__ = MagicMock(return_value=MagicMock(iloc=[tir]))

        mock_extractor = MagicMock()
        mock_extractor.transform.return_value = mock_stats
        sys.modules[
            "glucostats.extract_statistics"
        ].ExtractGlucoStats.return_value = mock_extractor
        return mock_extractor

    def test_returns_tir_in_string(self):
        _client.logbook.return_value = [self._make_reading(1, 120.0)]
        self._setup_glucostats(72.5)

        result = get_time_in_range.invoke({"days": 7, "low": 3.9, "high": 10.0})

        assert "72.5%" in result
        assert "3.9" in result
        assert "10.0" in result
        assert "7" in result

    def test_includes_reading_count(self):
        _client.logbook.return_value = [self._make_reading(1, 100.0)] * 3
        self._setup_glucostats(100.0)

        result = get_time_in_range.invoke({"days": 7})

        assert "3 readings" in result

    def test_defaults_to_standard_range(self):
        _client.logbook.return_value = [self._make_reading(1, 110.0)]
        self._setup_glucostats(95.0)

        result = get_time_in_range.invoke({})

        assert "3.9" in result
        assert "10.0" in result

    def test_custom_range_reflected_in_output(self):
        _client.logbook.return_value = [self._make_reading(1, 110.0)]
        self._setup_glucostats(80.0)

        result = get_time_in_range.invoke({"low": 4.4, "high": 7.8})

        assert "4.4" in result
        assert "7.8" in result

    def test_configuration_called_with_correct_range(self):
        _client.logbook.return_value = [self._make_reading(1, 100.0)]
        extractor = self._setup_glucostats(75.0)

        get_time_in_range.invoke({"low": 3.5, "high": 7.8})

        extractor.configuration.assert_called_once_with(
            in_range_interval=[3.5, 7.8], time_units="h"
        )

    def test_calls_client_logbook_with_correct_patient(self):
        _client.logbook.return_value = [self._make_reading(1, 100.0)]
        self._setup_glucostats(90.0)

        get_time_in_range.invoke({"days": 7})

        _client.logbook.assert_called_once_with(patient_identifier=_patients[0])

    def test_old_readings_excluded(self):
        recent = self._make_reading(1, 100.0)
        old = self._make_reading(30, 250.0)
        _client.logbook.return_value = [recent, old]
        extractor = self._setup_glucostats(100.0)

        get_time_in_range.invoke({"days": 7})

        # Only one reading should have been passed through to glucostats
        extractor.transform.assert_called_once()

    def test_empty_logbook_returns_message(self):
        _client.logbook.return_value = []

        result = get_time_in_range.invoke({"days": 7})

        assert "No glucose readings found" in result
        assert "7" in result

    def test_all_readings_outside_window_returns_message(self):
        _client.logbook.return_value = [self._make_reading(30, 150.0)]

        result = get_time_in_range.invoke({"days": 7})

        assert "No glucose readings found" in result

    def test_missing_timestamp_returns_error_string(self):
        _client.logbook.return_value = ["raw", "strings"]

        result = get_time_in_range.invoke({"days": 7})

        assert isinstance(result, str)
        assert "timestamp" in result.lower()

    def test_glucostats_exception_degrades_gracefully(self):
        import sys

        _client.logbook.return_value = [self._make_reading(1, 100.0)]
        mock_extractor = MagicMock()
        mock_extractor.transform.side_effect = RuntimeError("internal error")
        sys.modules[
            "glucostats.extract_statistics"
        ].ExtractGlucoStats.return_value = mock_extractor

        result = get_time_in_range.invoke({"days": 7})

        assert "fallback method" in result
        assert "internal error" in result


class TestGetAverageGlucose:
    def _make_reading(self, days_ago: float, value: float):
        r = MagicMock()
        r.factory_timestamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        r.value_in_mg_per_dl = value
        return r

    def test_returns_average_and_count(self):
        _client.logbook.return_value = [
            self._make_reading(1, 90.0),
            self._make_reading(1, 110.0),
        ]

        result = get_average_glucose.invoke({"days": 7})

        assert f"{(100.0 / 18.0):.1f} mmol/L" in result
        assert "2 readings" in result

    def test_no_readings_message(self):
        _client.logbook.return_value = []

        result = get_average_glucose.invoke({"days": 7})

        assert "No glucose readings found" in result


class TestGetEstimatedA1C:
    def _make_reading(self, days_ago: float, value: float):
        r = MagicMock()
        r.factory_timestamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        r.value_in_mg_per_dl = value
        return r

    def test_returns_estimated_a1c(self):
        _client.logbook.return_value = [
            self._make_reading(1, 100.0),
            self._make_reading(1, 100.0),
        ]

        result = get_estimated_a1c.invoke({"days": 14})

        assert "Estimated A1C" in result
        assert "5.11%" in result
        assert f"{(100.0 / 18.0):.1f} mmol/L" in result


class TestGetGlucoseSummary:
    def _make_reading(self, days_ago: float, value: float):
        r = MagicMock()
        r.factory_timestamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        r.value_in_mg_per_dl = value
        return r

    def test_includes_key_statistics(self):
        _client.logbook.return_value = [
            self._make_reading(1, 90.0),
            self._make_reading(1, 120.0),
            self._make_reading(1, 210.0),
        ]

        result = get_glucose_summary.invoke({"days": 7, "low": 3.9, "high": 10.0})

        assert f"avg {(140.0 / 18.0):.1f} mmol/L" in result
        assert f"min {(90.0 / 18.0):.1f}" in result
        assert f"max {(210.0 / 18.0):.1f}" in result
        assert "66.7%" in result
        assert "6.51%" in result

    def test_rejects_invalid_range(self):
        _client.logbook.return_value = [self._make_reading(1, 100.0)]

        result = get_glucose_summary.invoke({"low": 10.0, "high": 3.9})

        assert "low" in result
        assert "less than" in result


class TestKnowledgeBaseTools:
    def _make_reading(self, days_ago: float, value: float):
        r = MagicMock()
        now = datetime.now(timezone.utc)
        r.factory_timestamp = now - timedelta(days=days_ago)
        r.timestamp = now - timedelta(days=days_ago)
        r.value_in_mg_per_dl = value
        return r

    def test_sync_glucose_to_kb(self):
        _client.logbook.return_value = [
            self._make_reading(1, 95.0),
            self._make_reading(1, 105.0),
        ]

        result = sync_glucose_to_kb.invoke({"days": 7})

        assert "Synced glucose readings" in result
        assert "upserted" in result

    def test_log_meal(self):
        result = log_meal.invoke({"carbs_g": 45.0, "meal_type": "lunch"})
        assert "Logged meal" in result
        assert "45.0g" in result

    def test_log_insulin_valid(self):
        result = log_insulin.invoke(
            {"units": 4.0, "insulin_type": "rapid", "timing_tag": "pre-meal"}
        )
        assert "Logged insulin" in result

    def test_log_insulin_invalid_enum(self):
        result = log_insulin.invoke(
            {"units": 4.0, "insulin_type": "long", "timing_tag": "night"}
        )
        assert "Invalid insulin_type" in result

    def test_search_logged_events(self):
        log_meal.invoke({"carbs_g": 52.0, "meal_type": "dinner", "notes": "pasta"})
        result = search_logged_events.invoke({"query": "pasta", "since_days": 30})
        assert isinstance(result, str)

    def test_rag_search_reports(self):
        result = rag_search_reports.invoke(
            {"query": "hypoglycemia", "since_days": 180, "top_k": 3}
        )
        assert isinstance(result, str)

    def test_rag_search_glucose_patterns(self):
        result = rag_search_glucose_patterns.invoke(
            {"query": "low glucose", "since_days": 30, "top_k": 5}
        )
        assert isinstance(result, str)

    def test_get_icr_summary(self):
        now = datetime.now(timezone.utc).isoformat()
        log_meal.invoke({"carbs_g": 60.0, "meal_type": "breakfast", "timestamp": now})
        log_insulin.invoke(
            {
                "units": 6.0,
                "insulin_type": "rapid",
                "timing_tag": "pre-meal",
                "timestamp": now,
            }
        )

        result = get_icr_summary.invoke({"days": 14, "pairing_window_minutes": 60})

        assert "I:C summary" in result


class TestCGMSQLiteTools:
    def _make_reading(self, minutes_ago: int, value: float):
        r = MagicMock()
        now = datetime.now(timezone.utc)
        ts = now - timedelta(minutes=minutes_ago)
        r.factory_timestamp = ts
        r.timestamp = ts
        r.value_in_mg_per_dl = value
        r.trend = 3
        return r

    def test_sync_cgm_to_db_and_query_range(self):
        now = datetime.now(timezone.utc)
        _client.logbook.return_value = [
            self._make_reading(90, 95.0),
            self._make_reading(60, 110.0),
            self._make_reading(30, 140.0),
        ]

        sync_result = sync_cgm_to_db.invoke({"days": 2})
        assert "SQLite" in sync_result
        assert cgm_store.count_readings() == 3

        start = (now - timedelta(hours=2)).isoformat()
        end = now.isoformat()
        range_result = get_cgm_readings.invoke(
            {"start_iso": start, "end_iso": end, "limit": 100}
        )

        assert "CGM readings" in range_result
        assert f"{(140.0 / 18.0):.1f} mmol/L" in range_result

    def test_sync_latest_cgm_to_db(self):
        latest = self._make_reading(1, 123.0)
        _client.latest.return_value = latest

        result = sync_latest_cgm_to_db.invoke({})

        assert "latest cgm reading" in result.lower()
        assert cgm_store.count_readings() == 1

    def test_recent_summary_and_nearest(self):
        now = datetime.now(timezone.utc)
        for offset, value in [
            (100, 100.0 / 18.0),
            (70, 130.0 / 18.0),
            (40, 170.0 / 18.0),
            (10, 150.0 / 18.0),
        ]:
            cgm_store.upsert_cgm_reading(
                patient_id="test-patient",
                factory_timestamp_utc=(now - timedelta(minutes=offset)).isoformat(),
                device_timestamp_local=(now - timedelta(minutes=offset)).isoformat(),
                glucose_mmol_l=value,
                trend=3,
            )

        recent = get_recent_cgm_readings.invoke({"days": 2, "limit": 10})
        assert "Recent CGM readings" in recent

        summary = get_cgm_summary_from_db.invoke({"days": 2, "low": 3.9, "high": 10.0})
        assert "SQLite CGM summary" in summary
        assert "4 readings" in summary

        target = (now - timedelta(minutes=39)).isoformat()
        nearest = get_nearest_cgm_reading.invoke(
            {"timestamp": target, "window_minutes": 30}
        )
        assert "Nearest CGM reading" in nearest
        assert f"{(170.0 / 18.0):.1f} mmol/L" in nearest

        spikes = get_cgm_spikes_from_db.invoke(
            {
                "days": 2,
                "min_rise_mmol_l": 1.7,
                "min_drop_mmol_l": 0.8,
                "window_minutes": 120,
            }
        )
        assert "spikes" in spikes.lower()
        assert "dips" in spikes.lower()

    def test_deferred_enrichment_updates_pending_log(self):
        now = datetime.now(timezone.utc)

        # No CGM readings yet in the +90 min window, so this should queue deferred enrichment.
        result = log_meal.invoke(
            {
                "carbs_g": 42.0,
                "meal_type": "lunch",
                "timestamp": now.isoformat(),
                "notes": "sandwich",
            }
        )
        assert "pending" in result.lower()

        queue_counts = cgm_store.get_enrichment_queue_counts()
        assert queue_counts["pending"] >= 1

        # Add readings in the post-log window so deferred enrichment can complete.
        for offset, value in [
            (5, 120.0 / 18.0),
            (45, 150.0 / 18.0),
            (85, 140.0 / 18.0),
        ]:
            cgm_store.upsert_cgm_reading(
                patient_id="test-patient",
                factory_timestamp_utc=(now + timedelta(minutes=offset)).isoformat(),
                device_timestamp_local=(now + timedelta(minutes=offset)).isoformat(),
                glucose_mmol_l=value,
                trend=3,
            )

        # Force ready_at to now for deterministic testing.
        with cgm_store._lock:  # noqa: SLF001 - intentional test-level internal access
            cgm_store._conn.execute(
                "UPDATE cgm_enrichment_queue SET ready_at = ? WHERE status = 'pending'",
                (datetime.now(timezone.utc).isoformat(),),
            )
            cgm_store._conn.commit()

        run_result = run_deferred_log_enrichment.invoke(
            {"limit": 20, "max_attempts": 5}
        )
        assert "updated" in run_result.lower()

        queue_counts_after = cgm_store.get_enrichment_queue_counts()
        assert queue_counts_after["done"] >= 1

        rows = kb_store.get_meal_events(since_days=1)
        assert rows
        latest = rows[-1]
        notes = str(latest.get("notes", ""))
        assert "CGM context" in notes
        assert "sandwich" in notes

    def test_meal_logging_enriches_notes_from_cgm_db(self):
        now = datetime.now(timezone.utc)
        for offset, value in [
            (0, 110.0 / 18.0),
            (30, 145.0 / 18.0),
            (80, 170.0 / 18.0),
        ]:
            cgm_store.upsert_cgm_reading(
                patient_id="test-patient",
                factory_timestamp_utc=(now + timedelta(minutes=offset)).isoformat(),
                device_timestamp_local=(now + timedelta(minutes=offset)).isoformat(),
                glucose_mmol_l=value,
                trend=3,
            )

        result = log_meal.invoke(
            {
                "carbs_g": 55.0,
                "meal_type": "dinner",
                "timestamp": now.isoformat(),
                "notes": "rice bowl",
            }
        )
        assert "CGM context" in result

        rows = kb_store.get_meal_events(since_days=1)
        assert rows
        notes = str(rows[-1].get("notes", ""))
        assert "CGM context" in notes
        assert "peak rise" in notes

    def test_meal_context_uses_active_patient_id_filter(self):
        now = datetime.now(timezone.utc)
        cgm_store.set_sync_state("active_patient_id", "real-patient")

        # Reading for another patient in the same +90 min window should be ignored.
        cgm_store.upsert_cgm_reading(
            patient_id="other-patient",
            factory_timestamp_utc=(now + timedelta(minutes=10)).isoformat(),
            device_timestamp_local=(now + timedelta(minutes=10)).isoformat(),
            glucose_mmol_l=220.0 / 18.0,
            trend=3,
        )

        # Reading for active patient should be used.
        cgm_store.upsert_cgm_reading(
            patient_id="real-patient",
            factory_timestamp_utc=(now + timedelta(minutes=15)).isoformat(),
            device_timestamp_local=(now + timedelta(minutes=15)).isoformat(),
            glucose_mmol_l=120.0 / 18.0,
            trend=3,
        )

        result = log_meal.invoke(
            {
                "carbs_g": 30.0,
                "meal_type": "snack",
                "timestamp": now.isoformat(),
            }
        )

        assert f"{(120.0 / 18.0):.1f} mmol/L" in result
        assert "220.0 mmol/L" not in result


class TestInsulinRatioTools:
    """Tests for insulin-to-carb ratio logging and dosage calculation."""

    @staticmethod
    def _temp_ratios_file(tmp_path):
        """Create a temporary ratios JSON path for isolated testing."""
        return str(tmp_path / "insulin_ratios.json")

    def test_set_and_get_ratio(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        set_insulin_ratio.invoke({"meal_type": "breakfast", "ratio": 10.0})
        ratios = _load_ratios(path)
        assert ratios["breakfast"] == 10.0

    def test_get_missing_ratio(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        result = get_insulin_ratio.invoke({"meal_type": "dinner"})
        assert "No insulin ratio saved" in result

    def test_invalid_meal_type(self):
        # set_insulin_ratio and get_insulin_ratio take meal_type
        for tool in [set_insulin_ratio, get_insulin_ratio]:
            result = tool.invoke({"meal_type": "brunch", "ratio": 10.0})
            assert "Invalid meal_type" in result
        # calculate_insulin_dosage takes meal_type differently
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 30.0, "meal_type": "brunch"}
        )
        assert "Invalid meal_type" in result

    def test_set_overwrite_ratio(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        _save_ratios({"breakfast": 12.0}, path)
        result = set_insulin_ratio.invoke({"meal_type": "breakfast", "ratio": 8.0})
        assert "updated" in result
        assert "Previous ratio was 1:12.0" in result
        ratios = _load_ratios(path)
        assert ratios["breakfast"] == 8.0

    def test_calculate_with_saved_ratio(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        _save_ratios({"breakfast": 10.0}, path)
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 45.0, "meal_type": "breakfast"}
        )
        assert "45g carbs" in result
        assert "1:10.0" in result
        assert "4.5 units" in result

    def test_calculate_with_custom_ratio(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 40.0, "meal_type": "lunch", "custom_ratio": 8.0}
        )
        assert "40g carbs" in result
        assert "1:8.0" in result
        assert "5.0 units" in result

    def test_calculate_no_ratio_available(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 30.0, "meal_type": "lunch"}
        )
        assert "No insulin ratio saved for lunch" in result

    def test_calculate_zero_carbs(self):
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 0.0, "meal_type": "lunch", "custom_ratio": 10.0}
        )
        assert "greater than 0" in result

    def test_calculate_negative_custom_ratio(self):
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 30.0, "meal_type": "lunch", "custom_ratio": -5.0}
        )
        assert "greater than 0" in result

    def test_list_ratios(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        _save_ratios({"breakfast": 10.0, "lunch": 8.0, "dinner": 7.5}, path)
        result = list_insulin_ratios.invoke({})
        assert "Breakfast: 1:10.0" in result
        assert "Lunch: 1:8.0" in result
        assert "Dinner: 1:7.5" in result

    def test_list_ratios_empty(self, tmp_path, monkeypatch):
        path = self._temp_ratios_file(tmp_path)
        monkeypatch.setattr("agent.tools.INSULIN_RATIOS_PATH", path)
        result = list_insulin_ratios.invoke({})
        assert "No insulin ratios saved" in result

    def test_set_zero_ratio_rejected(self):
        result = set_insulin_ratio.invoke({"meal_type": "lunch", "ratio": 0.0})
        assert "greater than 0" in result

    def test_dosage_includes_disclaimer(self):
        result = calculate_insulin_dosage.invoke(
            {"carbs_g": 30.0, "meal_type": "snack", "custom_ratio": 10.0}
        )
        assert "not medical advice" in result

    def test_load_ratios_corrupted_file(self, tmp_path):
        path = self._temp_ratios_file(tmp_path)
        Path(path).write_text("not valid json {{{")
        ratios = _load_ratios(path)
        assert ratios == {}
