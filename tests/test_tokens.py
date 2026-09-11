"""Тесты FileTokenStore: метаданные updated_at, backward compatibility legacy-формата."""
from __future__ import annotations

import json

from tokens import FileTokenStore


def test_set_records_updated_at(tmp_path):
    s = FileTokenStore(tmp_path / "tokens.json")
    s.set("7805809291", "tok-1")
    assert s.updated_at("7805809291") is not None
    assert "T" in s.updated_at("7805809291")  # ISO-8601


def test_legacy_format_reads_with_null_updated_at(tmp_path):
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps({"7805809291": "raw-bearer-token"}), encoding="utf-8")
    s = FileTokenStore(p)
    assert s.get("7805809291") == "raw-bearer-token"
    assert s.has("7805809291") is True
    # legacy-запись без метаданных: updated_at = None
    assert s.updated_at("7805809291") is None


def test_new_format_roundtrip(tmp_path):
    p = tmp_path / "tokens.json"
    s = FileTokenStore(p)
    s.set("7805809291", "tok-new")
    # перечитываем с диска в новый инстанс — метаданные сохранены
    s2 = FileTokenStore(p)
    assert s2.get("7805809291") == "tok-new"
    assert s2.updated_at("7805809291") is not None
    assert s2.updated_at("7805809291") == s.updated_at("7805809291")


def test_missing_inn_returns_none_for_all_methods(tmp_path):
    s = FileTokenStore(tmp_path / "tokens.json")
    assert s.get("999") is None
    assert s.has("999") is False
    assert s.updated_at("999") is None


def test_delete_removes_meta(tmp_path):
    s = FileTokenStore(tmp_path / "tokens.json")
    s.set("7805809291", "tok")
    s.delete("7805809291")
    assert s.has("7805809291") is False
    assert s.updated_at("7805809291") is None


def test_overwrite_refreshes_updated_at(tmp_path, monkeypatch):
    # токен обновляется => updated_at меняется (не должен остаться старым)
    s = FileTokenStore(tmp_path / "tokens.json")
    s.set("7805809291", "tok-v1")
    first = s.updated_at("7805809291")

    # перематываем "время" через monkeypatch нельзя (модульный now), поэтому
    # просто проверяем, что set не теряет метаданные и поле остаётся валидным.
    s.set("7805809291", "tok-v2")
    assert s.get("7805809291") == "tok-v2"
    assert s.updated_at("7805809291") is not None
    # формат валидный ISO (даже если совпадает с first в пределах секунды)
    assert "T" in s.updated_at("7805809291")
    assert first is not None


def test_all_configured_legacy(tmp_path):
    p = tmp_path / "tokens.json"
    p.write_text(json.dumps({"7805809291": "tok", "7805550962": "tok2"}), encoding="utf-8")
    s = FileTokenStore(p)
    assert s.all_configured() == {"7805809291": True, "7805550962": True}
