import json
import tempfile
import time
import unittest
from pathlib import Path

from app.config import Settings
from app.services.performance import PerformanceRecorder, latest_performance_summary


class PerformanceOptimizationTests(unittest.TestCase):
    def test_performance_recorder_writes_required_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                openai_api_key=None,
                performance_debug_dir=Path(temporary_directory) / "performance_debug",
            )
            settings.performance_debug_dir.mkdir(parents=True, exist_ok=True)
            recorder = PerformanceRecorder(settings, "unit_test")
            with recorder.stage("resume_loading"):
                pass
            recorder.cache("search_plan", hit=True, resume_id=1)
            output_dir = recorder.save()

            expected_files = {
                "performance_summary.json",
                "stage_timings.json",
                "cache_usage.json",
                "page_load_times.json",
                "slow_operations.json",
            }
            self.assertTrue(expected_files.issubset({path.name for path in output_dir.iterdir()}))
            summary = json.loads((output_dir / "performance_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["cache_hits"], 1)
            self.assertGreaterEqual(summary["total_runtime_ms"], 0)

    def test_latest_performance_summary_returns_newest_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = Settings(
                openai_api_key=None,
                performance_debug_dir=Path(temporary_directory) / "performance_debug",
            )
            settings.performance_debug_dir.mkdir(parents=True, exist_ok=True)
            first = PerformanceRecorder(settings, "first")
            first.save()
            time.sleep(0.02)
            second = PerformanceRecorder(settings, "second")
            second.save()

            latest = latest_performance_summary(settings)
            self.assertIsNotNone(latest)
            self.assertEqual(latest["operation"], "second")
            self.assertTrue(latest["available"] if "available" in latest else True)


if __name__ == "__main__":
    unittest.main()
