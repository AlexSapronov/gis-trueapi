"""Read-only XML interface for legacy SQL Server clients."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from fastapi import Request, Response
from lxml import etree

from admin_auth import valid_key
from config import settings
from datamatrix import gtin_checksum_valid
from models import AppError, CATEGORY_MESSAGES, ErrorCategory

MAX_CODES = 100
MAX_BODY = 24 * 1024
KI_PATTERN = re.compile(r"01([0-9]{14})21[!-~]{20}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def xml_response(root: ET.Element, status: int = 200, headers=None) -> Response:
    return Response(ET.tostring(root, encoding="utf-8", xml_declaration=False),
                    status_code=status, media_type="application/xml; charset=utf-8",
                    headers={"Cache-Control": "no-store", **(headers or {})})


def error_response(status: int, code: str, message: str, headers=None) -> Response:
    return xml_response(ET.Element("Error", code=code, message=message), status, headers)


def read_codes(body: bytes) -> list[str]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(body, parser)
    if (root.getroottree().docinfo.doctype or root.tag != "Cises" or root.attrib
            or (root.text or "").strip()):
        raise ValueError
    if any(child.tag != "Cis" or child.attrib or len(child) or (child.tail or "").strip()
           for child in root):
        raise ValueError
    return [(child.text or "").strip() for child in root]


def valid_cis(cis: str) -> bool:
    match = KI_PATTERN.fullmatch(cis)
    return match is not None and gtin_checksum_valid(match[1])


def install_cis_check(app, service_getter):
    @app.post("/api/check-cis", response_class=Response)
    async def check_cis(request: Request) -> Response:
        key = settings.cis_check_key
        inn = settings.cis_check_org_inn
        if not KEY_PATTERN.fullmatch(key) or not inn:
            return error_response(503, "NOT_CONFIGURED", "Интеграция КИ не настроена")

        scheme, _, supplied_key = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not valid_key(supplied_key, key):
            return error_response(401, "UNAUTHORIZED", "Требуется ключ доступа к сервису",
                                  {"WWW-Authenticate": "Bearer"})

        media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if media_type not in {"application/xml", "text/xml"}:
            return error_response(415, "UNSUPPORTED_MEDIA_TYPE", "Передайте запрос в формате XML")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                return error_response(413, "BODY_TOO_LARGE", "Допустимо до 24 КиБ")
            body.extend(chunk)
        try:
            codes = read_codes(bytes(body))
        except (ValueError, etree.XMLSyntaxError):
            return error_response(400, "INVALID_XML", "Ожидается XML с элементом Cises и элементами Cis внутри")
        if not 1 <= len(codes) <= MAX_CODES:
            return error_response(400, "INVALID_COUNT", f"Передайте от 1 до {MAX_CODES} КИ")

        valid_codes = list(dict.fromkeys(cis for cis in codes if valid_cis(cis)))
        try:
            rows = await service_getter().lookup_cis_for_sql(valid_codes, inn) if valid_codes else {}
        except AppError as exc:
            code = exc.category
            status = {
                ErrorCategory.NO_ORG_TOKEN: 503,
                ErrorCategory.NOT_FOUND: 503,
                ErrorCategory.TOKEN_INVALID: 503,
                ErrorCategory.FORBIDDEN: 503,
                ErrorCategory.RATE_LIMIT: 503,
                ErrorCategory.TIMEOUT: 504,
            }.get(code, 502)
            return error_response(status, code.upper(), CATEGORY_MESSAGES.get(code, "Ошибка запроса в ЧЗ"))

        root = ET.Element("CisCheckResult")
        for index, cis in enumerate(codes, 1):
            row = ET.SubElement(root, "Row", index=str(index))
            if cis not in rows:
                ET.SubElement(row, "resultCode").text = "INVALID_CIS"
                ET.SubElement(row, "message").text = "Требуется 38-символьный КИ с корректным GTIN"
                continue
            ET.SubElement(row, "Cis").text = cis
            for field, value in rows[cis].items():
                ET.SubElement(row, field).text = XML_INVALID.sub("", str(value))
        return xml_response(root)
