import os
from unittest.mock import MagicMock, patch

from agent.reporter import generate_and_save_report


class TestGenerateAndSaveReport:
    def _make_mock_llm(self, draft="Draft report", critique_verdict="COMPLETE", feedback="Looks good"):
        mock_llm = MagicMock()
        responses = [
            MagicMock(content=draft),
            MagicMock(content=f'{{"verdict": "{critique_verdict}", "feedback": "{feedback}"}}'),
        ]
        mock_llm.invoke.side_effect = responses
        return mock_llm

    def test_creates_file_in_reports_dir(self, tmp_path):
        mock_llm = self._make_mock_llm("Weekly glucose summary.")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7)

        assert os.path.exists(path)

    def test_file_contains_report_text(self, tmp_path):
        mock_llm = self._make_mock_llm("Average glucose was 5.2 mmol/L.")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7)

        content = open(path).read()
        assert "Average glucose was 5.2 mmol/L." in content

    def test_file_header_includes_period(self, tmp_path):
        mock_llm = self._make_mock_llm("report")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=14)

        content = open(path).read()
        assert "14 days" in content

    def test_filename_includes_timestamp(self, tmp_path):
        mock_llm = self._make_mock_llm("report")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7)

        assert "glucose_report_" in os.path.basename(path)
        assert path.endswith(".txt")

    def test_returns_file_path(self, tmp_path):
        mock_llm = self._make_mock_llm("report")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            result = generate_and_save_report(period_days=7)

        assert isinstance(result, str)
        assert result.startswith(str(tmp_path))

    def test_indexes_report_in_kb(self, tmp_path):
        mock_llm = self._make_mock_llm("Indexed report text")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store") as mock_kb,
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7)

        mock_kb.add_report_document.assert_called_once()
        args, kwargs = mock_kb.add_report_document.call_args
        assert kwargs["file_path"] == path
        assert kwargs["period_days"] == 7

    def test_prints_metrics_line(self, tmp_path, capsys):
        mock_llm = self._make_mock_llm("report with metrics")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            generate_and_save_report(period_days=7)

        out = capsys.readouterr().out
        assert "[Metrics] report" in out

    def test_revises_when_critique_requests_changes(self, tmp_path):
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = [
            MagicMock(content="First draft with error"),
            MagicMock(content='{"verdict": "REVISE", "feedback": "Fix the A1C number"}'),
            MagicMock(content="Revised draft with correct A1C"),
            MagicMock(content='{"verdict": "COMPLETE", "feedback": "Good now"}'),
        ]
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7, max_iterations=2)

        content = open(path).read()
        assert "Revised draft with correct A1C" in content

    def test_uses_fallback_when_llm_fails(self, tmp_path):
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = TimeoutError("LLM hung")
        with (
            patch("agent.reporter.get_llm", return_value=mock_llm),
            patch("agent.reporter.REPORTS_DIR", str(tmp_path)),
            patch("agent.reporter.kb_store"),
            patch("agent.reporter.get_cgm_summary_from_db") as mock_summary,
            patch("agent.reporter.get_cgm_spikes_from_db") as mock_spikes,
            patch("agent.reporter.get_latest_glucose") as mock_latest,
        ):
            mock_summary.invoke.return_value = "Summary"
            mock_spikes.invoke.return_value = "Spikes"
            mock_latest.invoke.return_value = "Latest"
            path = generate_and_save_report(period_days=7)

        assert os.path.exists(path)
