"""Тесты live-клиента True API через httpx.MockTransport.

Проверяют: формирование запросов (URL, заголовки, body) и разбор ответов —
без реального доступа к ГИС МТ.
"""
import json

import httpx
import pytest

from trueapi import TrueApiClient, TrueApiError

BASE = "https://markirovka.crpt.ru"


def make_client(handler) -> TrueApiClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return TrueApiClient(BASE, http=http)


@pytest.mark.asyncio
async def test_exchange_token_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"token": "jwt-token-abc"})

    c = make_client(handler)
    token = await c.exchange_token("0000000001", "uuid-1", "signature-b64")
    assert token == "jwt-token-abc"
    assert captured["url"].endswith("/api/v3/true-api/auth/simpleSignIn")
    assert captured["body"]["uuid"] == "uuid-1"
    assert captured["body"]["data"] == "signature-b64"
    assert captured["body"]["inn"] == "0000000001"


@pytest.mark.asyncio
async def test_exchange_token_invalid_signature():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error_message": "Подпись невалидна. Код ошибки: 2"})

    c = make_client(handler)
    with pytest.raises(TrueApiError) as ei:
        await c.exchange_token("0000000001", "uuid", "bad")
    # 403 на auth => трактуем как проблема подписи
    assert ei.value.category in ("forbidden", "token_invalid")


@pytest.mark.asyncio
async def test_info_parses_cisinfo_and_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, list)
        return httpx.Response(
            200,
            json=[
                {
                    "cisInfo": {
                        "requestedCis": body[0],
                        "gtin": "04640638345218",
                        "productName": "M12D-04PFFS-SF8002",
                        "status": "APPLIED",
                        "ownerInn": "0000000001",
                        "quantityInPack": 400,
                        "generalPackageType": "UNIT",
                    }
                },
                {
                    "cisInfo": {"requestedCis": body[1], "gtin": "07712345678907"},
                    "errorMessage": "КИ не найден",
                    "errorCode": "404",
                },
            ],
        )

    c = make_client(handler)
    records = await c.info(["c1", "c2"], "tok")
    assert len(records) == 2
    assert records[0]["cisInfo"]["productName"] == "M12D-04PFFS-SF8002"
    assert records[0]["cisInfo"]["quantityInPack"] == 400
    assert records[1]["errorCode"] == "404"


@pytest.mark.asyncio
async def test_info_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error_message": "Токен не действителен"})

    c = make_client(handler)
    with pytest.raises(TrueApiError) as ei:
        await c.info(["c1"], "tok")
    assert ei.value.category == "token_invalid"


@pytest.mark.asyncio
async def test_search_body_and_pagination():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        # первая страница — не последняя, возвращает маркер
        if len(calls) == 1:
            return httpx.Response(
                200,
                json={
                    "isLastPage": False,
                    "result": [
                        {"sgtin": "04640638345218AAA", "cis": "04640638345218AAA",
                         "gtin": "04640638345218", "status": "APPLIED",
                         "emissionDate": "2026-01-01T00:00:00.000Z", "ownerInn": "0000000001"}
                    ],
                },
            )
        # вторая — последняя
        assert body["pagination"]["lastEmissionDate"] == "2026-01-01T00:00:00.000Z"
        assert body["pagination"]["sgtin"] == "04640638345218AAA"
        return httpx.Response(200, json={"isLastPage": True, "result": []})

    c = make_client(handler)
    page1 = await c.search("04640638345218", "APPLIED", "tok")
    assert page1["is_last"] is False
    assert page1["next"]["lastEmissionDate"] == "2026-01-01T00:00:00.000Z"

    # проверяем, что в первом запросе был корректный filter/pagination
    assert calls[0]["filter"]["gtins"] == ["04640638345218"]
    assert calls[0]["filter"]["states"] == [{"status": "APPLIED"}]

    page2 = await c.search("04640638345218", "APPLIED", "tok", after=page1["next"])
    assert page2["is_last"] is True


@pytest.mark.asyncio
async def test_check_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"result": True, "quantity": 2})

    c = make_client(handler)
    out = await c.check(["k1", "k2"], "tok")
    assert out["result"] is True
    assert captured["body"] == {"codes": ["k1", "k2"]}


@pytest.mark.asyncio
async def test_rate_limit_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429, json={"error_message": "Превышен лимит запросов"}, headers={"Retry-After": "5"}
        )

    c = make_client(handler)
    with pytest.raises(TrueApiError) as ei:
        await c.info(["c1"], "tok")
    assert ei.value.category == "rate_limit"