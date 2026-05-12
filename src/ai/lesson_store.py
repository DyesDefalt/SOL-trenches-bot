"""
JSON persistence untuk trade lessons dari ReflectionAgent.

Lessons disimpan sebagai JSON array di disk. Cap di max_lessons (FIFO).
Async file I/O via asyncio.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from src.infra.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PATH = Path("data/lessons.json")
_DEFAULT_MAX = 100


class LessonStore:
    """
    Persistent storage untuk trade lessons.

    Thread-safe via asyncio.Lock. Simpan di JSON file.
    FIFO cap di max_lessons (oldest lessons dihapus kalau over cap).
    """

    def __init__(
        self,
        path: Path = _DEFAULT_PATH,
        max_lessons: int = _DEFAULT_MAX,
    ) -> None:
        self._path = path
        self._max_lessons = max_lessons
        self._lock = asyncio.Lock()

    def _ensure_dir(self) -> None:
        """Buat parent directory kalau belum ada."""
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load_sync(self) -> list[dict]:
        """Load lessons dari disk. Return empty list kalau file tidak ada."""
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as e:
            log.warning("lesson_store_load_error", path=str(self._path), error=str(e))
            return []

    def _save_sync(self, lessons: list[dict]) -> None:
        """Save lessons ke disk."""
        self._ensure_dir()
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(lessons, f, indent=2, ensure_ascii=False, default=str)
        except OSError as e:
            log.error("lesson_store_save_error", path=str(self._path), error=str(e))

    async def add_lesson(self, lesson: dict) -> None:
        """
        Tambah lesson baru. Tambahkan timestamp kalau belum ada.
        Enforce FIFO cap di max_lessons.
        """
        async with self._lock:
            lessons = self._load_sync()

            # Tambah timestamp kalau belum ada
            if "timestamp" not in lesson:
                lesson = {"timestamp": datetime.now(UTC).isoformat(), **lesson}

            lessons.append(lesson)

            # FIFO cap — buang yang paling lama
            if len(lessons) > self._max_lessons:
                lessons = lessons[-self._max_lessons :]

            self._save_sync(lessons)
            log.debug(
                "lesson_stored",
                total=len(lessons),
                lesson_summary=lesson.get("lesson_summary", "")[:80],
            )

    async def get_recent(self, n: int = 5) -> list[dict]:
        """Return n most recent lessons (newest first)."""
        async with self._lock:
            lessons = self._load_sync()
            return list(reversed(lessons[-n:])) if lessons else []

    async def get_summary_strings(self, n: int = 5) -> list[str]:
        """Return list of 'lesson_summary' strings untuk prompt context."""
        recent = await self.get_recent(n)
        return [
            item.get("lesson_summary", "")
            for item in recent
            if item.get("lesson_summary")
        ]

    async def clear(self) -> None:
        """Clear semua lessons. Berguna untuk testing."""
        async with self._lock:
            self._save_sync([])
            log.info("lesson_store_cleared", path=str(self._path))
