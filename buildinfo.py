"""Определение build_id приложения.

Build ID — стабильная строка, идентифицирующая конкретный код приложения.
Не меняется между перезапусками без изменения кода, меняется после deploy.

Приоритет источников (первый доступный):
1. BUILD_ID из окружения (если задан явно).
2. Git short SHA текущего HEAD (без subprocess на каждый запрос —
   вычисляется один раз при импорте модуля).
3. Fallback: короткий hash списка исходников приложения (mtime + relative path
   *.py, *.js, *.css, *.html), когда .git недоступен. Меняется, если изменился
   backend ИЛИ frontend код.

Принципиально НЕ возвращаем случайный UUID — иначе простой restart сервиса
заставлял бы все ТСД обновляться без причины.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent

# Кешируем один раз на уровне процесса.
_BUILD_ID: str | None = None

# Расширения файлов, которые считаем исходниками приложения для fallback.
_SOURCE_SUFFIXES = (".py", ".js", ".css", ".html")

# Каталоги/файлы, которые НЕ считаем исходниками (не влияют на build_id fallback).
_EXCLUDED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
                  "node_modules", "data", "logs", ".hermes"}
_EXCLUDED_NAME_FRAGMENTS = ("tokens.json",)


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


def _iter_source_files() -> list[Path]:
    """Перечень релевантных исходников приложения (без runtime/caches/.git)."""
    files: list[Path] = []
    for p in _BASE_DIR.rglob("*"):
        # пропускаем исключённые каталоги
        rel_parts = p.relative_to(_BASE_DIR).parts
        if any(part in _EXCLUDED_DIRS for part in rel_parts):
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        if any(frag in p.name for frag in _EXCLUDED_NAME_FRAGMENTS):
            continue
        files.append(p)
    return files


def _source_hash() -> str:
    """Короткий детерминированный hash набора исходников.

    Стабилен между restart (зависит только от содержимого/mtime файлов),
    меняется при изменении backend ИЛИ frontend кода.
    """
    try:
        files = sorted(_iter_source_files(), key=lambda p: str(p))
        if not files:
            return "unknown"
        h = hashlib.sha1()
        for p in files:
            rel = str(p.relative_to(_BASE_DIR))
            # mtime + relative path достаточно: hash меняется при touch/deploy.
            st = p.stat()
            h.update(rel.encode("utf-8", "replace"))
            h.update(b"\x00")
            h.update(str(int(st.st_mtime)).encode())
            h.update(b"\x00")
            h.update(str(st.st_size).encode())
            h.update(b"\x00")
        return f"src-{h.hexdigest()[:10]}"
    except OSError:
        return "unknown"


def get_build_id() -> str:
    """Возвращает build_id. Вычисляет один раз, дальше отдаёт кеш."""
    global _BUILD_ID
    if _BUILD_ID is None:
        env = os.getenv("BUILD_ID", "").strip()
        if env:
            _BUILD_ID = env
        else:
            _BUILD_ID = _git_short_sha() or _source_hash()
    return _BUILD_ID
