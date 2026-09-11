"""Интеграционный тест /api/status: build_id + token_updated_at.

Проверяет, что статус-эндпоинт отдаёт build_id и метаданные возраста токена
(нужны для frontend auto-reload и индикатора возраста токена).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import main as main_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    # изолированный токен-стор + mock-режим
    from tokens import FileTokenStore
    from mockclient import MockTrueApiClient
    from service import Service

    store = FileTokenStore(tmp_path / "tokens.json")
    store.set("0000000001", "mock-token")
    svc = Service(MockTrueApiClient(), store)
    monkeypatch.setattr(main_module, "svc", svc)
    return TestClient(main_module.app)


def test_status_has_build_id_and_token_meta(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    d = r.json()
    assert "build_id" in d
    assert d["build_id"]  # непустой

    orgs = d["organizations"]
    kombr = [o for o in orgs if o["inn"] == "0000000001"][0]
    assert kombr["token_configured"] is True
    assert "token_updated_at" in kombr
    assert kombr["token_updated_at"] is not None  # только что записан — meta есть


def test_status_legacy_token_has_null_updated_at(monkeypatch, tmp_path):
    """Старый формат tokens.json (строка) -> token_updated_at = None, не падает."""
    import json
    from tokens import FileTokenStore
    from mockclient import MockTrueApiClient
    from service import Service

    p = tmp_path / "legacy.json"
    p.write_text(json.dumps({"0000000001": "raw-token"}), encoding="utf-8")
    store = FileTokenStore(p)
    svc = Service(MockTrueApiClient(), store)
    monkeypatch.setattr(main_module, "svc", svc)

    c = TestClient(main_module.app)
    d = c.get("/api/status").json()
    kombr = [o for o in d["organizations"] if o["inn"] == "0000000001"][0]
    assert kombr["token_configured"] is True
    assert kombr["token_updated_at"] is None
