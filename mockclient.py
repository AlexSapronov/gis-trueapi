"""Mock-реализация клиента True API для разработки и тестов без доступа к ЧЗ.

Возвращает структуры, максимально похожие на live-клиент TrueApiClient.
"""
from __future__ import annotations

import re
from typing import Any

from models import ErrorCategory
from trueapi import TrueApiError


class MockTrueApiClient:
    """Демо-клиент. Не обращается в сеть."""

    STATUSES = ["EMITTED", "APPLIED", "INTRODUCED"]
    our_gtin = "04640638345218"

    async def info(self, cises: list[str], token: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for cis in cises:
            gtin, serial = self._split(cis)
            if not gtin:
                results.append(
                    {
                        "cisInfo": {"requestedCis": cis},
                        "errorMessage": "Некорректный КМ",
                        "errorCode": "400",
                    }
                )
                continue

            if "NOTFOUND" in serial:
                results.append(
                    {
                        "cisInfo": {"requestedCis": cis, "gtin": gtin, "packageType": None},
                        "errorMessage": "КИ не найден",
                        "errorCode": "404",
                    }
                )
                continue

            ours = gtin == self.our_gtin
            owner_inn = "7805809291" if ours else "7770000000"
            owner_name = 'ООО "КОМБРИ"' if ours else 'ООО "Сторонний поставщик"'

            status = "APPLIED"
            if "EMITTED" in serial:
                status = "EMITTED"
            elif "INTRODUCED" in serial:
                status = "INTRODUCED"

            quantity = 400
            m = re.search(r"Q(\d+)", serial)
            if m:
                quantity = int(m.group(1))

            results.append(
                {
                    "cisInfo": {
                        "requestedCis": cis,
                        "cis": cis,
                        "gtin": gtin,
                        "productName": "M12D-04PFFS-SF8002",
                        "status": status,
                        "statusEx": "EMPTY",
                        "ownerInn": owner_inn,
                        "ownerName": owner_name,
                        "producerInn": "7805000000",
                        "generalPackageType": "UNIT",
                        "quantityInPack": quantity,
                    },
                    "errorMessage": None,
                    "errorCode": None,
                }
            )
        return results

    @staticmethod
    def _split(cis: str) -> tuple[str | None, str]:
        """Разбирает cis (либо GTIN+serial, либо 01<GTIN>21<serial>) → (gtin, serial)."""
        if not cis:
            return None, ""
        # форма с AI: 01 + 14 цифр + 21 + serial
        m = re.match(r"01(\d{14})21(.*)$", cis)
        if m:
            return m.group(1), m.group(2)
        # форма GTIN+serial
        if len(cis) >= 14:
            return cis[:14], cis[14:]
        return None, cis

    async def search(
        self,
        gtin: str,
        status: str,
        token: str,
        per_page: int = 1000,
        after: dict[str, Any] | None = None,
        product_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        if status == "EMITTED":
            count = 12
        elif status == "APPLIED":
            count = 17
        elif status == "INTRODUCED":
            count = 5
        else:
            count = 0

        # простейшая пагинация: если передан маркер after — считаем что уже всё выдали
        if after:
            return {"items": [], "is_last": True, "next": None}

        items = []
        for i in range(count):
            cis = f"{gtin}{status[:3].upper()}{i:03d}"
            items.append(
                {
                    "sgtin": cis,
                    "cis": cis,
                    "gtin": gtin,
                    "status": status,
                    "emissionDate": f"2026-01-0{(i % 9) + 1}T00:00:00.000Z",
                }
            )
        return {"items": items, "is_last": True, "next": None}

    async def check(self, cises: list[str], token: str) -> dict[str, Any]:
        return {"result": True, "quantity": len(cises)}

    async def exchange_token(self, inn: str, uuid: str, signature: str) -> str:
        if not uuid or not signature:
            raise TrueApiError(ErrorCategory.VALIDATION, "Пустой UUID или SIGNATURE", status=400)
        if "BAD" in signature.upper():
            raise TrueApiError(ErrorCategory.TOKEN_INVALID, "Неверная подпись", status=403)
        return f"mock-token-{uuid[:8]}-{hash(signature) & 0xFFFF:04x}"