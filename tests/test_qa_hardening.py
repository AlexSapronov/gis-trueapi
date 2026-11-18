"""QA-hardening regressions: True API batching-edge and Service pairing guidance.

Each test checks a real observable contract (not mere import success) and is
isolated from any True API network access via MockTransport / MockTrueApiClient.
"""
import json

import httpx
import pytest

from datamatrix import parse
from mockclient import MockTrueApiClient
from service import Service
from tokens import FileTokenStore
from trueapi import INFO_PATH, MAX_INFO_BATCH, TrueApiClient, TrueApiError

VALID_GTIN = "04640638345218"
ORG = "0000000001"


# ---- True API client: batching beyond the documented 1000-KI limit ----

@pytest.mark.asyncio
async def test_info_chunks_over_1000_and_concatenates_in_order():
    """cises/info capacity is 1000; larger lists must be split into several
    requests (not sent in one oversized body) and results recombined.
    """
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body, list)
        assert len(body) <= MAX_INFO_BATCH, "single info() request exceeded the 1000-KI limit"
        sent.append(body)
        out = []
        for cis in body:
            out.append({"cisInfo": {"requestedCis": cis, "cis": cis, "status": "INTRODUCED"}})
        return httpx.Response(200, json=out)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TrueApiClient("https://trueapi.test", http=http)
    cises = [f"01046406383452182{i:05d}" for i in range(MAX_INFO_BATCH + 7)]
    records = await client.info(cises, "tok")
    # request split, all KIs echoed back exactly once, in order
    assert len(sent) == 2
    assert [len(s) for s in sent] == [MAX_INFO_BATCH, 7]
    got = [r["cisInfo"]["requestedCis"] for r in records]
    assert got == cises


@pytest.mark.asyncio
async def test_info_concatenates_all_success_chunks_when_one_succeeds():
    """A later chunk must not be dropped just because an earlier chunk is a
    per-KI-error body returned with HTTP 404 (CZ reachable, KI missing)."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body and body[0].endswith("99999"):
            return httpx.Response(404, json=[{"cisInfo": {"requestedCis": body[0]},
                                              "errorMessage": "КИ не найден", "errorCode": "404"}])
        return httpx.Response(200, json=[{"cisInfo": {"requestedCis": c, "cis": c}} for c in body])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TrueApiClient("https://trueapi.test", http=http)
    cises = [f"01046406383452182{i:05d}" for i in range(MAX_INFO_BATCH)] + [
        "0104640638345218299999"]
    records = await client.info(cises, "tok")
    assert calls and len(calls) == 2
    assert [r.get("errorCode") for r in records if "errorCode" in r] == ["404"]
    assert any("errorCode" not in r for r in records)


@pytest.mark.asyncio
async def test_info_short_cis_without_prefix_is_passed_verbatim():
    """service passes normalized_cis (GTIN+serial) only when api_cis is absent;
    the client must not guess prefixes. Verify genuine record for the CI is kept."""
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json=[{"cisInfo": {"requestedCis": c, "cis": c}} for c in body])

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TrueApiClient("https://trueapi.test", http=http)
    await client.info(["01" + VALID_GTIN + "21SERIAL1"], "tok")
    assert sent[0] == ["01" + VALID_GTIN + "21SERIAL1"]


# ---- Service: response-order independence and scanner-prefix hygiene ----

def _svc_with_records(records):
    class Client(MockTrueApiClient):
        async def info(self, cises, token):
            # Deliberately echo records in REVERSE order to prove pairing is by
            # requestedCis, never by position (True API may reorder).
            return [r for r in records if r["cisInfo"]["requestedCis"] in cises][::-1]
    tokens = FileTokenStore.__new__(FileTokenStore)
    return Service(Client(), tokens)


@pytest.mark.asyncio
async def test_scan_batch_pairs_by_identity_not_by_order(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")
    records = [
        {"cisInfo": {"requestedCis": "01" + VALID_GTIN + "21AAA", "gtin": VALID_GTIN,
                     "status": "INTRODUCED", "quantityInPack": 5, "ownerInn": ORG}},
        {"cisInfo": {"requestedCis": "01" + VALID_GTIN + "21BBB", "gtin": VALID_GTIN,
                     "status": "INTRODUCED", "quantityInPack": 7, "ownerInn": ORG}},
    ]

    class Reversing(MockTrueApiClient):
        async def info(self, cises, token):
            return [r for r in records if r["cisInfo"]["requestedCis"] in cises][::-1]

    svc = Service(Reversing(), tokens)
    results = await svc.scan_batch(["01" + VALID_GTIN + "21AAA", "01" + VALID_GTIN + "21BBB"], ORG)
    assert [r.serial for r in results] == ["AAA", "BBB"]
    assert [r.quantity_in_pack for r in results] == [5, 7]  # not swapped


@pytest.mark.asyncio
async def test_scan_batch_api_cis_never_leaks_scanner_prefix(tmp_path):
    """A symbology prefix must never reach the True API request body."""
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")
    sent = []

    class Capturing(MockTrueApiClient):
        async def info(self, cises, token):
            sent.extend(cises)
            return await super().info(cises, token)

    svc = Service(Capturing(), tokens)
    base = "01" + VALID_GTIN + "21SERIAL1"
    await svc.scan_batch(["]d2" + base, "^]" + base, "]C1" + base], ORG)
    assert sent == [base, base, base]
    assert all(not c.startswith("]") for c in sent)


@pytest.mark.parametrize("prefix", ["]d2", "]C1", "^]"])
def test_parse_api_cis_never_contains_any_scanner_prefix(prefix):
    p = parse(prefix + "01" + VALID_GTIN + "21SERIAL")
    assert p.api_cis == "01" + VALID_GTIN + "21SERIAL"
    assert not p.api_cis.startswith("]")


@pytest.mark.asyncio
async def test_scan_single_misattributed_record_is_rejected(tmp_path):
    """A single /api/scan whose info record does not correspond to the requested
    CI must not be silently accepted as a success for that CI."""
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class WrongEcho(MockTrueApiClient):
        async def info(self, cises, token):
            return [{"cisInfo": {"requestedCis": c + "_NOT_THE_ONE", "gtin": "07712345678907",
                                 "status": "INTRODUCED", "ownerInn": ORG}} for c in cises]

    svc = Service(WrongEcho(), tokens)
    result = await svc.scan("01" + VALID_GTIN + "21SERIAL", ORG)
    # The requested CI got no matching record => not "ours", no positive verdict
    assert result.error_category is not None and "не найден" in (result.error_message or "").lower()


# ---- True API error surfaces never become a positive verdict ----

@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500])
@pytest.mark.asyncio
async def test_info_http_error_surfaces_as_categorized_failure(tmp_path, status):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class Failing(MockTrueApiClient):
        async def info(self, cises, token):
            raise TrueApiError("api_error" if status not in (401, 429, 500) else
                               ("token_invalid" if status == 401 else
                                ("rate_limit" if status == 429 else "api_server_error")),
                               "upstream fail", status)

    svc = Service(Failing(), tokens)
    result = await svc.scan("01" + VALID_GTIN + "21SERIAL", ORG)
    assert result.api_checked is True
    assert result.error_category is not None
    assert result.error_message
    # a failed API must never fabricate INTRODUCED / ours=True
    assert result.status is None and result.ours is None
