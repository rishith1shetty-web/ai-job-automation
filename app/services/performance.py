import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from app.config import Settings


@dataclass
class PerformanceRecorder:
    settings: Settings
    operation: str
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        safe_operation = "".join(character if character.isalnum() or character in "-_" else "_" for character in self.operation)
        self.output_dir = self.settings.performance_debug_dir / f"{timestamp}_{safe_operation}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.started_at = time.perf_counter()
        self.stage_timings: list[dict] = []
        self.cache_usage: list[dict] = []
        self.page_load_times: list[dict] = []

    @contextmanager
    def stage(self, name: str, **metadata) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            self.stage_timings.append(
                {
                    "stage": name,
                    "elapsed_ms": elapsed_ms,
                    "metadata": metadata,
                }
            )
            if "page" in name.lower() or "linkedin" in name.lower() or "apply" in name.lower():
                self.page_load_times.append(
                    {
                        "stage": name,
                        "elapsed_ms": elapsed_ms,
                        "metadata": metadata,
                    }
                )

    def cache(self, name: str, hit: bool, **metadata) -> None:
        self.cache_usage.append(
            {
                "cache": name,
                "hit": hit,
                "metadata": metadata,
            }
        )

    def save(self) -> Path:
        total_ms = round((time.perf_counter() - self.started_at) * 1000, 2)
        slow_operations = sorted(
            [item for item in self.stage_timings if item["elapsed_ms"] >= 1000],
            key=lambda item: item["elapsed_ms"],
            reverse=True,
        )
        slowest = slow_operations[0] if slow_operations else (max(self.stage_timings, key=lambda item: item["elapsed_ms"]) if self.stage_timings else None)
        summary = {
            "operation": self.operation,
            "metadata": self.metadata,
            "total_runtime_ms": total_ms,
            "stage_count": len(self.stage_timings),
            "cache_hits": sum(1 for item in self.cache_usage if item["hit"]),
            "cache_misses": sum(1 for item in self.cache_usage if not item["hit"]),
            "average_page_load_ms": self._average(self.page_load_times),
            "average_apply_time_ms": self._average([item for item in self.stage_timings if "application" in item["stage"]]),
            "average_screening_time_ms": self._average([item for item in self.stage_timings if "screening" in item["stage"]]),
            "average_employment_history_time_ms": self._average([item for item in self.stage_timings if "employment" in item["stage"]]),
            "slowest_operation": slowest,
            "created_at": datetime.utcnow().isoformat(),
        }
        self._write("performance_summary.json", summary)
        self._write("stage_timings.json", self.stage_timings)
        self._write("cache_usage.json", self.cache_usage)
        self._write("page_load_times.json", self.page_load_times)
        self._write("slow_operations.json", slow_operations)
        return self.output_dir

    def _average(self, items: list[dict]) -> float:
        if not items:
            return 0
        return round(sum(float(item["elapsed_ms"]) for item in items) / len(items), 2)

    def _write(self, filename: str, payload) -> None:
        (self.output_dir / filename).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def latest_performance_summary(settings: Settings) -> dict | None:
    if not settings.performance_debug_dir.exists():
        return None
    directories = sorted(
        [path for path in settings.performance_debug_dir.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for directory in directories:
        path = directory / "performance_summary.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            payload["evidence_path"] = str(directory)
            return payload
    return None
