"""Хранилище bearer-токенов по организациям.

Файловая реализация для первой версии. Архитектура позволяет заменить на
другой бэкенд (БД, in-memory) без изменения Service — достаточно реализации
того же интерфейса.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional, Protocol


class TokenStore(Protocol):
    def get(self, inn: str) -> Optional[str]: ...
    def set(self, inn: str, token: str) -> None: ...
    def has(self, inn: str) -> bool: ...
    def all_configured(self) -> dict[str, bool]: ...


class FileTokenStore:
    """Потокобезопасное файловое хранилище токенов (data/tokens.json)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._cache = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._cache = {k: v for k, v in data.items() if isinstance(v, str)}
        except (json.JSONDecodeError, OSError):
            self._cache = {}

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ограничиваем правами владельца (0600) на POSIX
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._cache, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def get(self, inn: str) -> Optional[str]:
        with self._lock:
            return self._cache.get(inn)

    def set(self, inn: str, token: str) -> None:
        with self._lock:
            self._cache[inn] = token
            self._persist()

    def has(self, inn: str) -> bool:
        with self._lock:
            return inn in self._cache

    def delete(self, inn: str) -> None:
        with self._lock:
            self._cache.pop(inn, None)
            self._persist()

    def all_configured(self) -> dict[str, bool]:
        with self._lock:
            return {k: True for k in self._cache}