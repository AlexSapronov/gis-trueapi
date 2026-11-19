import json
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cis_check import install_cis_check
from config import settings
from models import ErrorCategory
from service import Service
from tokens import FileTokenStore
from trueapi import INFO_PATH, PRODUCT_INFO_PATH, SHORT_LIST_PATH, TrueApiClient, TrueApiError

GTIN = "04640638345218"
PREFIX = "01" + GTIN + "21"
CIS = PREFIX + "ABC1234567890123&<ab"
OTHER = PREFIX + "0" * 20
NOT_FOUND = PREFIX + "1" * 20
ORG = "0000000001"
DT = "10131020/061026/5125218"
HEADERS = {"Authorization": "Bearer " + "k" * 43, "Content-Type": "application/xml"}


class Api:
    def __init__(self):
        self.info_calls = []

    async def info(self, codes, token):
        assert token == "private-upstream-token"
        self.info_calls.append(codes)
        records = []
        for cis in reversed(codes):
            if cis == NOT_FOUND:
                records.append({"requestedCis": cis, "errorCode": "404"})
                continue
            details = {"requestedCis": cis, "gtin": GTIN, "status": "INTRODUCED", "statusEx": "EMPTY",
                       "productName": "Компонент A&B", "productGroup": "radio", "generalPackageType": "UNIT",
                       "quantityInPack": 6000, "country": "076", "tnVedEaes": "8536699008", "brand": "Test",
                       "producerInn": "0123456789", "producerName": "Производитель", "ownerInn": ORG,
                       "ownerName": "ООО Владелец"}
            if cis == OTHER:
                details.update(quantityInPack=None, declaration={"declarationRegNumber": DT,
                                                                 "declarationDate": "2026-10-07"})
            records.append({"cisInfo": details})
        return records

    async def short_info(self, codes, token):
        return [{"result": {"requestedCis": cis, "gtin": GTIN, "specialAttributes": {
            "approvementDocument": {"declarationRegNumber": DT, "declarationDate": "06.10.2026"}}}}
                for cis in codes]

    async def product_info(self, gtins, token):
        assert token == "private-upstream-token"
        assert gtins == [GTIN]
        return [{"gtin": GTIN, "model": "PN-1"}]


@pytest.fixture
def integration(monkeypatch, tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "private-upstream-token")
    service = Service(Api(), tokens)
    monkeypatch.setattr(settings, "cis_check_key", "k" * 43)
    monkeypatch.setattr(settings, "cis_check_org_inn", ORG)
    app = FastAPI()
    install_cis_check(app, lambda: service)
    return TestClient(app), service


def xml_body(codes):
    root = ET.Element("Cises")
    for cis in codes:
        ET.SubElement(root, "Cis").text = cis
    return ET.tostring(root)


def post(client, codes):
    return client.post("/api/check-cis", content=xml_body(codes), headers=HEADERS)


def test_xml_returns_complete_fields_in_input_order_including_duplicates(integration):
    client, service = integration
    response = post(client, [CIS, OTHER, CIS, "invalid", NOT_FOUND])
    assert response.status_code == 200
    assert service.client.info_calls == [[CIS, OTHER, NOT_FOUND]]
    root = ET.fromstring(response.content)
    assert [row.get("index") for row in root] == ["1", "2", "3", "4", "5"]
    assert [row.findtext("resultCode") for row in root] == ["OK", "PARTIAL", "OK", "INVALID_CIS", "NOT_FOUND"]
    fields = {field.tag: field.text for field in root[0]}
    assert fields == {"Cis": CIS, "GTIN": GTIN, "quantityInPack": "6000", "declarationRegNumber": DT,
                      "declarationDate": "2026-10-06", "status": "INTRODUCED", "tnVedEaes": "8536699008",
                      "country": "076", "packageType": "UNIT", "productName": "PN-1", "brand": "Test",
                      "product_group": "radio", "producerInn": "0123456789", "producerName": "Производитель",
                      "ownerInn": ORG, "ownerName": "ООО Владелец", "statusEx": "EMPTY", "PN": "PN-1",
                      "resultCode": "OK"}
    assert root[1].find("quantityInPack") is None
    assert root[1].findtext("message") == "ЧЗ не вернул корректные поля: quantityInPack"
    assert root[1].findtext("declarationDate") == "2026-10-07"


def test_product_name_falls_back_to_true_api_name_when_article_is_unavailable(integration):
    client, service = integration

    async def no_article(gtins, token):
        return [{"gtin": GTIN}]

    service.client.product_info = no_article
    response = post(client, [CIS])
    assert response.status_code == 200
    row = ET.fromstring(response.content)[0]
    assert row.findtext("productName") == "Компонент A&B"
    assert row.find("PN") is None


@pytest.mark.asyncio
async def test_receipt_with_2000_cis_uses_true_api_batches_and_returns_all_rows(integration):
    client, service = integration
    counts = {INFO_PATH: [], SHORT_LIST_PATH: [], PRODUCT_INFO_PATH: []}

    def upstream(request):
        path = request.url.path
        batch = json.loads(request.content)
        if path == PRODUCT_INFO_PATH:
            batch = batch["gtins"]
        counts[path].append(len(batch))
        if path == PRODUCT_INFO_PATH:
            return httpx.Response(200, json={"results": [{"gtin": gtin, "model": "PN-" + gtin} for gtin in batch]})
        if path == SHORT_LIST_PATH:
            return httpx.Response(200, json=[{"result": {"requestedCis": cis, "gtin": cis[2:16],
                "specialAttributes": {"approvementDocument": {"declarationRegNumber": DT,
                                                              "declarationDate": "06.10.2026"}}}}
                for cis in reversed(batch)])
        return httpx.Response(200, json=[{"cisInfo": {"requestedCis": cis, "gtin": cis[2:16],
            "status": "INTRODUCED", "quantityInPack": 6000}} for cis in reversed(batch)])

    codes = []
    for index in range(2000):
        prefix = f"{4640638345000 + index:013d}"
        weighted = sum(int(digit) * weight for digit, weight in zip(reversed(prefix), [3, 1] * 7))
        gtin = prefix + str(-weighted % 10)
        codes.append(f"01{gtin}21A&<{index:017d}")

    body = xml_body(codes)
    assert len(body) > 100_000
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as api_http:
        service.client = TrueApiClient("http://true-api.test", http=api_http)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url="http://service.test") as http:
            response = await http.post("/api/check-cis", content=body, headers=HEADERS)

    assert response.status_code == 200
    assert all(batches == [1000, 1000] for batches in counts.values())
    rows = ET.fromstring(response.content).findall("Row")
    assert [row.findtext("Cis") for row in rows] == codes
    assert all(row.findtext("quantityInPack") == "6000" and row.findtext("declarationRegNumber") == DT
               and row.findtext("productName") == "PN-" + cis[2:16] for row, cis in zip(rows, codes))


def test_sql_route_checks_key_before_xml_and_rejects_dtd(integration, monkeypatch):
    client, service = integration
    url = "/api/check-cis"
    assert client.post(url, content=b"malformed").status_code == 401
    assert client.post(url, content=b"<Cises/>", headers={**HEADERS, "Authorization": "Bearer wrong"}).status_code == 401
    dtd = b'<!DOCTYPE Cises [<!ENTITY x SYSTEM "file:///etc/passwd">]><Cises><Cis>&x;</Cis></Cises>'
    assert client.post(url, content=dtd, headers=HEADERS).status_code == 400
    assert client.post(url, content=b"x" * (512 * 1024 + 1), headers=HEADERS).status_code == 413
    assert post(client, [CIS] * 2001).status_code == 400
    monkeypatch.setattr(settings, "cis_check_key", "")
    assert post(client, [CIS]).status_code == 503
    assert service.client.info_calls == []


def test_batch_failure_does_not_expose_upstream_response(integration):
    client, service = integration

    async def upstream_failure(codes, token):
        raise TrueApiError(ErrorCategory.TOKEN_INVALID, "private error contents")

    service.client.info = upstream_failure
    response = post(client, [CIS])
    assert response.status_code == 503
    assert ET.fromstring(response.content).attrib["code"] == "TOKEN_INVALID"
    assert "private error contents" not in response.text
