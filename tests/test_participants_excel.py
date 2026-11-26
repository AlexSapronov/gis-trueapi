import io
from types import SimpleNamespace

import httpx
import openpyxl
import pytest

from participants_excel import classify, lookup_batch, read_rows, render_workbook, UNKNOWN, INVALID


def test_classification():
    assert classify({"is_registered": True, "productGroups": ["radio"]}) == ("Да", "Да")
    assert classify({"is_registered": True, "productGroups": ["electronics", "gadgets"]}) == ("Да", "Нет")
    assert classify({"is_registered": True, "status": "Заблокирован", "productGroups": []}) == ("Да (заблокирован)", "Нет")
    assert classify({"error_code": "404"}) == ("Нет", "Нет")
    assert classify({"error_code": "400"}) == (INVALID, INVALID)
    assert classify({"is_registered": True}) == ("Да", UNKNOWN)
    assert classify({"is_registered": False, "productGroups": []}) == ("Нет", "Нет")
    assert classify({}) == (UNKNOWN, UNKNOWN)


def test_excel_preserves_rows_and_reuses_columns():
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["Полное наименование", "ИНН", "Примечание"])
    sheet.append(["A", "0123456789", "сохранить"])
    sheet.append(["B", 1234567890, "другое"])
    sheet.append(["A повтор", "0123456789"])
    sheet.append(["Пустой ИНН"])
    book.create_sheet("Другой лист")["A1"] = "Не трогать"
    rows = read_rows(book)
    assert [r[-1] for r in rows] == ["0123456789", "1234567890", "0123456789", ""]
    data, report = render_workbook(book, rows, {"0123456789": ("Да", "Да"), "1234567890": ("Да", "Нет")})
    result = openpyxl.load_workbook(io.BytesIO(data))
    assert result.active["C2"].value == "сохранить"
    assert result.active["D1"].value == "Зарегистрирован"
    assert result.active["E1"].value == "Подключен РЭП"
    assert result.active["E4"].value == "Да"
    assert result.active["D5"].value == INVALID
    assert result["Другой лист"]["A1"].value == "Не трогать"
    assert report["radio"] == 2 and report["unique"] == 2
    assert read_rows(result)[0][3] == (4, 5)


@pytest.mark.asyncio
async def test_public_batch_request_matches_inn_not_order():
    def handler(request):
        assert request.url.path == "/api/v3/true-api/participants"
        assert request.url.params.get_list("inns") == ["0123456789", "1234567890", "1111111111"]
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[
            {"inn": "1234567890", "error_code": "404"},
            {"inn": "0123456789", "is_registered": True, "productGroups": ["radio"]},
        ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await lookup_batch(SimpleNamespace(http=http, base_url="https://example.invalid"),
                                    ["0123456789", "1234567890", "1111111111"])
    assert result == {"1234567890": ("Нет", "Нет"), "0123456789": ("Да", "Да")}


@pytest.mark.asyncio
async def test_http_failure_not_negative():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503))) as http:
        with pytest.raises(ValueError):
            await lookup_batch(SimpleNamespace(http=http, base_url="https://example.invalid"), ["0123456789"])
