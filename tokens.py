"""Хранилище bearer-токенов по организациям.

Файловая реализация для первой версии. Архитектура позволяет заменить на
другой бэкенд (БД, in-memory) без изменения Service — достаточно реализации
того же интерфейса.

Формат файла (data/tokens.json) — одна из двух форм (оба читаются):

    Новый (с метаданными):
        {"0000000001": {"token": "...", "updated_at": "2026-09-11T05:30:00Z"}}

    Старый (только токен) — backward compatible:
        {"0000000001": "bearer-token-string"}

При записи используется новый формат. Старые записи читаются как есть,
метаданные (updated_at) для них = None.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TokenStore(Protocol):
    def get(self, inn: str) -> Optional[str]: ...
    def set(self, inn: str, token: str) -> None: ...
    def has(self, inn: str) -> bool: ...
    def all_configured(self) -> dict[str, bool]: ...
    def updated_at(self, inn: str) -> Optional[str]: ...


class FileTokenStore:
    """Потокобезопасное файловое хранилище токенов (data/tokens.json)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        # Внутренний кеш: inn -> {"token": str, "updated_at": str|None}
        self._cache: dict[str, dict[str, Optional[str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self._cache = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._cache = {}
            return
        self._cache = {}
        for inn, val in raw.items():
            if isinstance(val, str):
                # legacy: {"inn": "token"}
                self._cache[inn] = {"token": val, "updated_at": None}
            elif isinstance(val, dict):
                token = val.get("token")
                if not isinstance(token, str) or not token:
                    continue
                updated_at = val.get("updated_at")
                self._cache[inn] = {
                    "token": token,
                    "updated_at": updated_at if isinstance(updated_at, str) else None,
                }

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            inn: {"token": rec["token"], "updated_at": rec["updated_at"]}
            for inn, rec in self._cache.items()
        }
        # ограничиваем правами владельца (0600) на POSIX
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def get(self, inn: str) -> Optional[str]:
        with self._lock:
            rec = self._cache.get(inn)
            return rec["token"] if rec else None

    def set(self, inn: str, token: str) -> None:
        with self._lock:
            self._cache[inn] = {"token": token, "updated_at": _utc_now_iso()}
            self._persist()

    def has(self, inn: str) -> bool:
        with self._lock:
            return inn in self._cache

    def delete(self, inn: str) -> None:
        with self._lock:
            self._cache.pop(inn, None)
            self._persist()

    def updated_at(self, inn: str) -> Optional[str]:
        """UTC ISO 8601 timestamp последнего обновления токена (или None для legacy)."""
        with self._lock:
            rec = self._cache.get(inn)
            return rec["updated_at"] if rec else None

    def all_configured(self) -> dict[str, bool]:
        with self._lock:
            return {k: True for k in self._cache}
