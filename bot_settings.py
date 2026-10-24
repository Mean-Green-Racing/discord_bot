"""Persistent, per-server bot settings (no new service or dependency)."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile


DEFAULT_SOURCES = 3
MIN_SOURCES = 1
MAX_SOURCES = 10


class BotSettings:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.max_sources = DEFAULT_SOURCES
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                count = raw.get("max_sources")
                if isinstance(count, int) and not isinstance(count, bool) and MIN_SOURCES <= count <= MAX_SOURCES:
                    self.max_sources = count
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                print(f"Bot settings invalid; using default source count: {exc}")

    def set_sources(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or not MIN_SOURCES <= count <= MAX_SOURCES:
            raise ValueError("Source count must be an integer between 1 and 10")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"max_sources": count}, indent=2) + "\n"
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent,
                                             prefix="bot-settings-", suffix=".json", delete=False) as handle:
                temp_name = Path(handle.name)
                handle.write(payload)
            temp_name.replace(self.path)
        finally:
            if temp_name is not None:
                temp_name.unlink(missing_ok=True)
        self.max_sources = count
