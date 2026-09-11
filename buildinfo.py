"""Определение build_id приложения.

Build ID — стабильная строка, идентифицирующая конкретный код приложения.
Не меняется между перезапусками без изменения кода, меняется после deploy.

Приоритет источников (первый доступный):
1. BUILD_ID из окружения (если задан явно).
2. Git short SHA текущего HEAD (без subprocess на каждый запрос —
   вычисляется один раз при импорте модуля).
3. mtime самого молодого .py файла проекта (fallback, меняется при deploy).

Принципиально НЕ возвращаем случайный UUID — иначе простой restart сервиса
заставлял бы все ТСД обновляться без причины.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent

# Кешируем один раз на уровне процесса.
_BUILD_ID: str | None = None


def _git_short_sha() -> str | None:
    """Git short SHA без обращения к сети. Безопасно (не падает), если .git нет."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_BASE_DIR),
            capture_output=True,
            text=True,
            timeout=2,
        )
        sha = out.stdout.strip()
        if out.returncode == 0 and sha:
            return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _newest_py_mtime() -> str:
    """Fallback: время изменения самого свежего .py файла проекта.

    Меняется при разворачивании нового кода (git pull), поэтому служит
    стабильным маркером версии, когда .git недоступен.
    """
    try:
        newest = max(_BASE_DIR.rglob("*.py"), key=lambda p: p.stat().st_mtime)
        return f"mtime({newest.name}:{int(newest.stat().st_mtime)})"
    except (OSError, ValueError):
        # пустой/недоступный каталог — последний резерв
        return "unknown"


def get_build_id() -> str:
    """Возвращает build_id. Вычисляет один раз, дальше отдаёт кеш."""
    global _BUILD_ID
    if _BUILD_ID is None:
        env = os.getenv("BUILD_ID", "").strip()
        if env:
            _BUILD_ID = env
        else:
            _BUILD_ID = _git_short_sha() or _newest_py_mtime()
    return _BUILD_ID
