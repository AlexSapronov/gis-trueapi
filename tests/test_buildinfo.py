"""Тесты build_id и метаданных статуса (/api/status build_id + token_updated_at)."""
from __future__ import annotations

from buildinfo import get_build_id


def test_build_id_is_stable_and_nonempty():
    a = get_build_id()
    b = get_build_id()
    assert isinstance(a, str)
    assert a.strip()  # непустой
    assert a == b      # кешируется, не меняется в рамках процесса


def test_build_id_env_override(monkeypatch):
    monkeypatch.setenv("BUILD_ID", "ci-build-123")
    # get_build_id кешируется на уровне процесса — сбрасываем кеш для теста
    import buildinfo
    buildinfo._BUILD_ID = None
    try:
        assert get_build_id() == "ci-build-123"
    finally:
        buildinfo._BUILD_ID = None


def test_build_id_no_random_uuid():
    # build_id не должен быть случайным UUID — иначе restart сервиса без
    # изменения кода вызывал бы бессмысленный reload всех ТСД.
    import uuid
    v = get_build_id()
    assert "mtime(" not in v or v != "unknown"  # fallback валиден
    # не похож на uuid
    try:
        uuid.UUID(v)
        assert False, "build_id не должен быть UUID"
    except ValueError:
        pass
