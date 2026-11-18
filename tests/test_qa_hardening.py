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
from trueapi import MAX_INFO_BATCH, TrueApiClient, TrueApiError

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
    cises = [f"01{VALID_GTIN}21{i:05d}" for i in range(MAX_INFO_BATCH + 7)]
    records = await client.info(cises, "tok")
    # request split, all KIs echoed back exactly once, in order
    assert len(sent) == 2
    assert [len(s) for s in sent] == [MAX_INFO_BATCH, 7]
    got = [r["cisInfo"]["requestedCis"] for r in records]
    assert got == cises


@pytest.mark.asyncio
async def test_info_continues_after_first_chunk_returns_per_ki_errors():
    """A 404 error-array for the first 1000 KIs must not discard the next chunk."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(
                404,
                json=[
                    {"cisInfo": {"requestedCis": cis},
                     "errorMessage": "КИ не найден", "errorCode": "404"}
                    for cis in body
                ],
            )
        return httpx.Response(
            200,
            json=[{"cisInfo": {"requestedCis": cis, "cis": cis, "gtin": VALID_GTIN}}
                  for cis in body],
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TrueApiClient("https://trueapi.test", http=http)
    cises = [f"01{VALID_GTIN}21{i:05d}" for i in range(MAX_INFO_BATCH + 7)]
    records = await client.info(cises, "tok")

    assert len(calls) == 2
    assert [len(chunk) for chunk in calls] == [MAX_INFO_BATCH, 7]
    assert len(records) == len(cises)
    assert all(record.get("errorCode") == "404" for record in records[:MAX_INFO_BATCH])
    assert all("errorCode" not in record for record in records[MAX_INFO_BATCH:])
    assert [record["cisInfo"]["requestedCis"] for record in records] == cises


@pytest.mark.asyncio
async def test_info_sends_normalized_cis_without_ai_prefix_verbatim():
    """The low-level client must not rewrite an already-normalized GTIN+serial."""
    sent = []
    normalized_cis = VALID_GTIN + "SERIAL1"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json=[{"cisInfo": {"requestedCis": cis, "cis": cis}} for cis in body],
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = TrueApiClient("https://trueapi.test", http=http)
    await client.info([normalized_cis], "tok")
    assert sent == [[normalized_cis]]


# ---- Service: response-order independence and scanner-prefix hygiene ----

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
    # A reply claiming an unrelated CI is a malformed upstream response.
    assert result.error_category == "api_error"
    assert result.status is None and result.ours is None



@pytest.mark.asyncio
async def test_scan_single_rejects_conflicting_identity_fields(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")
    requested = "01" + VALID_GTIN + "21SERIAL"

    class ConflictingEcho(MockTrueApiClient):
        async def info(self, cises, token):
            return [{
                "cisInfo": {
                    "requestedCis": requested,
                    "cis": "01" + VALID_GTIN + "21SOMEONE_ELSE",
                    "gtin": VALID_GTIN,
                    "status": "INTRODUCED",
                    "ownerInn": ORG,
                }
            }]

    result = await Service(ConflictingEcho(), tokens).scan(requested, ORG)
    assert result.error_category == "api_error"
    assert result.status is None and result.ours is None


@pytest.mark.asyncio
async def test_scan_single_rejects_duplicate_matching_records(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class DuplicateEcho(MockTrueApiClient):
        async def info(self, cises, token):
            record = {
                "cisInfo": {
                    "requestedCis": cises[0],
                    "cis": cises[0],
                    "gtin": VALID_GTIN,
                    "status": "INTRODUCED",
                    "ownerInn": ORG,
                }
            }
            return [record, record.copy()]

    result = await Service(DuplicateEcho(), tokens).scan(
        "01" + VALID_GTIN + "21SERIAL", ORG
    )
    assert result.error_category == "api_error"
    assert result.status is None and result.ours is None


@pytest.mark.asyncio
async def test_scan_single_rejects_gtin_mismatch_even_when_identity_matches(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class WrongGtin(MockTrueApiClient):
        async def info(self, cises, token):
            return [{
                "cisInfo": {
                    "requestedCis": cises[0],
                    "cis": cises[0],
                    "gtin": "07712345678907",
                    "status": "INTRODUCED",
                    "ownerInn": ORG,
                }
            }]

    result = await Service(WrongGtin(), tokens).scan(
        "01" + VALID_GTIN + "21SERIAL", ORG
    )
    assert result.error_category == "api_error"
    assert result.status is None and result.ours is None


@pytest.mark.asyncio
async def test_scan_single_accepts_normalized_cis_echo(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class NormalizedEcho(MockTrueApiClient):
        async def info(self, cises, token):
            return [{
                "cisInfo": {
                    "requestedCis": VALID_GTIN + "SERIAL",
                    "gtin": VALID_GTIN,
                    "status": "INTRODUCED",
                    "ownerInn": ORG,
                }
            }]

    result = await Service(NormalizedEcho(), tokens).scan(
        "01" + VALID_GTIN + "21SERIAL", ORG
    )
    assert result.error_category is None
    assert result.status == "INTRODUCED"
    assert result.ours is True


@pytest.mark.asyncio
async def test_scan_batch_rejects_ambiguous_record_identity(tmp_path):
    tokens = FileTokenStore(tmp_path / "tokens.json")
    tokens.set(ORG, "tok")

    class ConflictingEcho(MockTrueApiClient):
        async def info(self, cises, token):
            return [{
                "cisInfo": {
                    "requestedCis": cises[0],
                    "cis": "01" + VALID_GTIN + "21NOT_THE_REQUESTED_CI",
                    "gtin": VALID_GTIN,
                    "status": "INTRODUCED",
                    "ownerInn": ORG,
                }
            }]

    results = await Service(ConflictingEcho(), tokens).scan_batch(
        ["01" + VALID_GTIN + "21AAA", "01" + VALID_GTIN + "21BBB"], ORG
    )
    assert all(result.error_category == "api_error" for result in results)
    assert all(result.status is None and result.ours is None for result in results)


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
