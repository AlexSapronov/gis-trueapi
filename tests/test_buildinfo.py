"""Тесты build_id: приоритет источников, fallback по исходникам, стабильность."""
from __future__ import annotations

import buildinfo
from buildinfo import get_build_id, _git_short_sha, _source_hash


def _reset_cache():
    buildinfo._BUILD_ID = None


def test_build_id_is_stable_and_nonempty():
    a = get_build_id()
    b = get_build_id()
    assert isinstance(a, str)
    assert a.strip()  # непустой
    assert a == b      # кешируется, не меняется в рамках процесса


def test_build_id_env_override(monkeypatch):
    monkeypatch.setenv("BUILD_ID", "ci-build-123")
    _reset_cache()
    try:
        assert get_build_id() == "ci-build-123"
    finally:
        _reset_cache()


def test_build_id_no_random_uuid():
    import uuid
    v = get_build_id()
    try:
        uuid.UUID(v)
        assert False, "build_id не должен быть UUID"
    except ValueError:
        pass


def test_source_hash_is_stable_and_deterministic():
    a = _source_hash()
    b = _source_hash()
    assert a == b  # детерминированный для неизменного дерева
    assert a.startswith("src-")


def test_source_hash_sees_frontend_and_backend_files():
    """Fallback учитывает .py, .js, .css, .html — т.е. и backend, и frontend."""
    files = [str(p.relative_to(buildinfo._BASE_DIR)) for p in buildinfo._iter_source_files()]
    # backend-исходники присутствуют
    assert any(f.endswith(".py") for f in files)
    # frontend-исходники присутствуют
    assert any(f.endswith(".js") for f in files)
    assert any(f.endswith(".css") for f in files)
    assert any(f.endswith(".html") for f in files)


def test_source_hash_excludes_runtime(tmp_path, monkeypatch):
    """Исключает .git/.venv/data/logs/caches из перечня исходников."""
    files = buildinfo._iter_source_files()
    rel = [str(p.relative_to(buildinfo._BASE_DIR)) for p in files]
    # ни одного runtime-файла
    assert not any("tokens.json" in f for f in rel)
    assert not any(f.startswith(".venv") or f.startswith("venv/") for f in rel)
    assert not any("__pycache__" in f for f in rel)
    assert not any(f.startswith(".git") for f in rel)
    assert not any(f.startswith("data/") for f in rel)


def test_git_short_sha_matches_current_head():
    """В репозитории build_id = git short SHA (основной источник)."""
    sha = get_build_id()
    # если .git доступен — это hex-строка вида короткого SHA
    head = _git_short_sha()
    if head is not None:
        assert sha == head
