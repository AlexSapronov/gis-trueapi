"""Extract customs declaration data from True API short/list records."""
from __future__ import annotations

from typing import Any


def interpret_gtd_record(record: dict[str, Any], cis: str, gtin: str) -> dict[str, Any]:
    """The absence of a DT number never implies that the product was not imported."""
    if not isinstance(record, dict):
        raise ValueError("Некорректная запись ответа True API")

    if record.get("errorMessage"):
        return {
            "found": False, "cis": cis, "gtin": gtin,
            "message": str(record["errorMessage"]),
            "declaration_number": None, "declaration_date": None,
            "declaration_id": None,
        }

    details = record.get("result")
    if details is None:
        details = record.get("cisInfo")
    if not isinstance(details, dict) or not details:
        raise ValueError("True API не вернул сведения о КИ")
    if isinstance(details.get("cisInfo"), dict):
        details = details["cisInfo"]

    attrs = details.get("specialAttributes")
    if not isinstance(attrs, dict):
        attrs = {}
    docs = attrs.get("approvementDocument")
    if not isinstance(docs, dict):
        docs = {}

    # Do not mistake certDoc (conformity certificates) for a customs declaration.
    number = docs.get("declarationRegNumber") or None
    date = docs.get("declarationDate") or None
    declaration_id = docs.get("declarationId") or None

    return {
        "found": True,
        "cis": cis,
        "gtin": details.get("gtin") or gtin,
        "product_name": details.get("productName"),
        "status": details.get("status"),
        "emission_type": details.get("emissionType"),
        "introduced_date": attrs.get("introducedDate"),
        "declaration_number": number,
        "declaration_date": date,
        "declaration_id": declaration_id,
        "message": (
            "Номер ДТ найден" if number
            else "Код найден, но номер ДТ в доступных сведениях True API не указан. Это не означает, что декларации нет."
        ),
    }
