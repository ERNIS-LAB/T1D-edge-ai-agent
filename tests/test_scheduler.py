# tests/test_scheduler.py
import os
from unittest.mock import patch

import pytest

import run_scheduler
from config import REPORT_PERIOD_DAYS


class TestReportJob:
    def test_uses_config_period_by_default(self):
        os.environ.pop("REPORT_PERIOD_DAYS", None)
        with patch.object(run_scheduler, "generate_and_save_report") as mock_gen:
            run_scheduler._report_job()
        mock_gen.assert_called_once_with(period_days=REPORT_PERIOD_DAYS)

    def test_env_var_overrides_period(self):
        with patch.object(run_scheduler, "generate_and_save_report") as mock_gen, \
             patch.dict(os.environ, {"REPORT_PERIOD_DAYS": "14"}):
            run_scheduler._report_job()
        mock_gen.assert_called_once_with(period_days=14)

    def test_env_var_takes_int(self):
        with patch.object(run_scheduler, "generate_and_save_report") as mock_gen, \
             patch.dict(os.environ, {"REPORT_PERIOD_DAYS": "30"}):
            run_scheduler._report_job()
        # period_days must be an int, not the string "30"
        _, kwargs = mock_gen.call_args
        assert isinstance(kwargs["period_days"], int)
