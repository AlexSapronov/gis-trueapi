"""Tests for the administrator-only customs declaration lookup."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from config import settings
from gtd import interpret_gtd_record
from mockclient import MockTrueApiClient
from models import AppError, ErrorCategory
from service import Service
from tokens import FileTokenStore
from trueapi import SHORT_LIST_PATH, TrueApiClient, TrueApiError

GTIN = "04640638345218"
CIS = "01" + GTIN + "21ABC123"
DT = "10000000/060820/0000000"


def sample_record():
    return {"result": {
        "requestedCis": CIS, "cis": CIS, "gtin": GTIN,
        "productName": "Импортный товар", "status": "INTRODUCED",
        "emissionType": "FOREIGN",
        "specialAttributes": {
            "introducedDate": "2026-11-01T10:00:00Z",
            "approvementDocument": {
                "declarationRegNumber": DT,
                "declarationDate": "06.08.2020",
                "declarationId": "some-doc-id",
                "certDoc": [{"number": "СЕРТИФИКАТ-НЕ-ДТ"}],
            },
        },
    }}


@pytest.mark.asyncio
async def test_short_list_uses_authorized_post_and_array_body():
    seen = {}
    def handler(request):
        seen["path"] = request.url.path
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[sample_record()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TrueApiClient("https://markirovka.crpt.ru", http=http)
        result = await client.short_info([CIS], "secret")
    assert seen == {"path": SHORT_LIST_PATH, "auth": "Bearer secret", "body": [CIS]}
    assert result[0]["result"]["specialAttributes"]["approvementDocument"]["declarationRegNumber"] == DT


@pytest.mark.asyncio
async def test_short_list_rejects_non_list():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"result": []})
    )) as http:
        with pytest.raises(TrueApiError):
            await TrueApiClient("https://markirovka.crpt.ru", http=http).short_info([CIS], "token")


def test_record_does_not_confuse_certificate_with_dt():
    record = sample_record()
    assert interpret_gtd_record(record, CIS, GTIN)["declaration_number"] == DT
    del record["result"]["specialAttributes"]["approvementDocument"]["declarationRegNumber"]
    result = interpret_gtd_record(record, CIS, GTIN)
    assert result["found"] and result["declaration_number"] is None
    assert "не указан" in result["message"]


def test_not_found_is_distinct_from_no_dt():
    response = interpret_gtd_record({"errorMessage": "КИ не найден", "errorCode": "404"}, CIS, GTIN)
    assert response["found"] is False
    assert response["message"] == "КИ не найден"


class RecordingClient(MockTrueApiClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def short_info(self, cises, token):
        self.calls.append((cises, token))
        return [sample_record()]


@pytest.fixture
def service(tmp_path):
    client = RecordingClient()
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set("0000000001", "my-token")
    return Service(client, tokens)


@pytest.mark.asyncio
async def test_gtd_accepts_scanner_gs_fnc_and_trailing_ai30(service):
    code = "]d2" + CIS + "<GS>91EE1234<GS>92abcdef<GS>3012"
    result = await service.lookup_gtd(code, "0000000001")
    assert result["declaration_number"] == DT
    assert result["cis"] == CIS
    assert service.client.calls == [([CIS], "my-token")]


@pytest.mark.asyncio
async def test_gtd_rejects_invalid_code_without_trueapi_call(service):
    with pytest.raises(AppError) as ex:
        await service.lookup_gtd("not a code", "0000000001")
    assert ex.value.category == ErrorCategory.DM_STRUCTURE
    assert service.client.calls == []


@pytest.mark.asyncio
async def test_gtd_rejects_unknown_organization(service):
    with pytest.raises(AppError) as ex:
        await service.lookup_gtd(CIS, "9999999999")
    assert ex.value.category == ErrorCategory.NOT_FOUND
    assert service.client.calls == []


@pytest.mark.asyncio
async def test_upstream_404_is_not_reported_as_missing_organization(service):
    async def failed_short_info(cises, token):
        raise TrueApiError(ErrorCategory.NOT_FOUND, 'Not Found', status=404)
    service.client.short_info = failed_short_info
    with pytest.raises(AppError) as error:
        await service.lookup_gtd(CIS, '0000000001')
    assert error.value.category == ErrorCategory.API_ERROR
    assert 'True API' in error.value.message
    assert 'HTTP 404' in error.value.message

def test_admin_gtd_endpoint_is_protected_and_responds(monkeypatch, service):
    monkeypatch.setattr(settings, "admin_key", "gtd-test-admin-key")
    monkeypatch.setattr(main, "svc", service)
    with TestClient(main.app) as client:
        url = "/api/admin/warehouse/gtd"
        body = {"code": CIS, "org_inn": "0000000001"}
        assert client.post(url, json=body).status_code == 403
        assert client.post("/api/admin/session", json={"key": "gtd-test-admin-key"}).status_code == 200
        response = client.post(url, json=body, headers={"X-Admin-Request": "1"})
        assert response.status_code == 200
        assert response.json()["declaration_number"] == DT
        assert "my-token" not in response.text
        assert client.post(url, json={"code": "bad"}, headers={"X-Admin-Request": "1"}).status_code == 422
