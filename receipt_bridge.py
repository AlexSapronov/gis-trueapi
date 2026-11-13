"""Scoped marking-bm ingress. One durable source identity per receipt."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import date

from config import settings
from fastapi import HTTPException, Request
from organizations import registry
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ReceiptCode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw: str = Field(min_length=1, max_length=4096)
    pn: str = Field(min_length=1, max_length=1000)
    quantity: int = Field(strict=True, gt=0, le=2147483647)
    atk: str = Field(pattern=r"^[0-9]{20}[0-9A-Za-z]{5}$")


class ReceiptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_instance: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    source_document: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    revision: int = Field(strict=True, ge=1)
    number: str = Field(min_length=1, max_length=300)
    date: date
    org_inn: str = Field(pattern=r"^[0-9]{10,12}$")
    supplier: str = Field(min_length=1, max_length=1000)
    codes: list[ReceiptCode] = Field(max_length=50000)


def prepare(payload):
    from warehouse_excel import gtin_checksum_valid, scan_data
    from warehouse_store import WarehouseError

    org = registry.get(payload.org_inn)
    if not org:
        raise WarehouseError("ИНН организации marking-bm отсутствует в gis-trueapi")
    lines, expected, seen = {}, [], set()
    for item in payload.codes:
        code = scan_data(item.raw)
        if not code["has_crypto"] or not gtin_checksum_valid(code["gtin"]):
            raise WarehouseError(
                "Для приёмки нужен полный КМ с корректным GTIN и крипточастью"
            )
        if code["cis"] in seen:
            raise WarehouseError("Дубликат КМ в составе накладной")
        seen.add(code["cis"])
        pn = item.pn.strip()
        if not pn:
            raise WarehouseError("Заполните PN")
        line = lines.setdefault(
            code["gtin"],
            {"gtin": code["gtin"], "pn": pn, "pn_source": "marking-bm", "quantity": 0},
        )
        if line["pn"] != pn:
            raise WarehouseError("У одного GTIN указаны разные PN")
        line["quantity"] += item.quantity
        if line["quantity"] > 2147483647:
            raise WarehouseError("Слишком большое количество товара")
        expected.append({**code, "quantity": item.quantity})
    return org, list(lines.values()), expected


class ReceiptBridgeStore:
    @staticmethod
    def init_receipt_bridge(c):
        c.execute("""CREATE TABLE IF NOT EXISTS warehouse_receipt_sources (
            source_instance TEXT NOT NULL, source_document TEXT NOT NULL,
            document_id TEXT NOT NULL UNIQUE REFERENCES warehouse_documents(id),
            revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, metadata TEXT NOT NULL,
            PRIMARY KEY(source_instance,source_document))""")

    def sync_receipt(self, payload):
        from warehouse_store import WarehouseError, now

        org, lines, expected = prepare(payload)
        encoded = payload.model_dump_json()
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connection(True) as c:
            source = c.execute(
                "SELECT * FROM warehouse_receipt_sources WHERE source_instance=? AND source_document=?",
                (payload.source_instance, payload.source_document),
            ).fetchone()
            if source:
                document_id = source["document_id"]
                d = self._doc(c, document_id, include_deleted=True)
                if d["deleted_at"] or d["archived_at"]:
                    raise WarehouseError(
                        "Связанная приёмка в корзине или архиве; требуется администратор",
                        409,
                    )
                if payload.revision < source["revision"]:
                    raise WarehouseError("Устаревшая версия состава", 409)
                if payload.revision == source["revision"]:
                    if fingerprint != source["fingerprint"]:
                        raise WarehouseError("Та же версия содержит другой состав", 409)
                    return {
                        "document_id": document_id,
                        "revision": payload.revision,
                        "status": "unchanged",
                    }
                if (
                    d["status"] != "draft"
                    or c.execute(
                        "SELECT 1 FROM warehouse_scans WHERE document_id=? LIMIT 1",
                        (document_id,),
                    ).fetchone()
                ):
                    raise WarehouseError(
                        "Приёмка уже запущена. Изменение состава требует разбора администратором; сканы сохранены",
                        409,
                    )
                if d["check_id"] and (d["check_until"] or 0) > time.time():
                    raise WarehouseError("Приёмка проверяется; повторите позже", 409)
                c.execute(
                    "DELETE FROM warehouse_expected WHERE document_id=?", (document_id,)
                )
                c.execute(
                    "DELETE FROM warehouse_lines WHERE document_id=?", (document_id,)
                )
                c.execute(
                    """UPDATE warehouse_documents SET number=?,date=?,org_inn=?,org_name=?,
                    result=NULL,result_version=NULL,result_at=NULL WHERE id=?""",
                    (
                        payload.number,
                        payload.date.isoformat(),
                        org.inn,
                        org.name,
                        document_id,
                    ),
                )
                self._bump(c, document_id)
                status = "updated"
            else:
                if not expected:
                    raise WarehouseError("Нельзя создать пустую приёмку")
                document_id, ts = uuid.uuid4().hex, now()
                c.execute(
                    """INSERT INTO warehouse_documents
                    (id,kind,number,date,org_inn,org_name,recipient_name,recipient_inn,strict_owner,created_at,updated_at)
                    VALUES (?,'receipt',?,?,?,?, '','',1,?,?)""",
                    (
                        document_id,
                        payload.number,
                        payload.date.isoformat(),
                        org.inn,
                        org.name,
                        ts,
                        ts,
                    ),
                )
                status = "created"
            for line in lines:
                c.execute(
                    "INSERT INTO warehouse_lines VALUES (?,?,?,?,?)",
                    (
                        document_id,
                        line["gtin"],
                        line["pn"],
                        line["pn_source"],
                        line["quantity"],
                    ),
                )
            for e in expected:
                c.execute(
                    "INSERT INTO warehouse_expected VALUES (?,?,?,?,?,?)",
                    (
                        document_id,
                        e["cis"],
                        e["raw_code"],
                        e["full_code"],
                        e["gtin"],
                        e["quantity"],
                    ),
                )
            metadata = json.dumps(
                {
                    "supplier": payload.supplier,
                    "atks": sorted({i.atk for i in payload.codes}),
                    "source_document": payload.source_document,
                },
                ensure_ascii=False,
            )
            c.execute(
                """INSERT INTO warehouse_receipt_sources VALUES (?,?,?,?,?,?)
                ON CONFLICT(source_instance,source_document) DO UPDATE SET
                revision=excluded.revision,fingerprint=excluded.fingerprint,metadata=excluded.metadata""",
                (
                    payload.source_instance,
                    payload.source_document,
                    document_id,
                    payload.revision,
                    fingerprint,
                    metadata,
                ),
            )
            self._event(
                c,
                document_id,
                "receipt_sync",
                {"revision": payload.revision, "status": status},
                actor="marking-bm",
            )
        return {
            "document_id": document_id,
            "revision": payload.revision,
            "status": status,
        }


def install_receipt_bridge(app, store_getter):
    @app.post("/api/integrations/marking-bm/receipts")
    async def receive(request: Request):
        key = settings.receipt_bridge_key
        supplied = request.headers.get("X-Receipt-Bridge-Key", "")
        if (
            not key
            or len(key) < 32
            or not hmac.compare_digest(supplied.encode(), key.encode())
        ):
            raise HTTPException(403, "Доступ запрещён")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 32 * 1024 * 1024:
                raise HTTPException(413, "Превышен размер состава")
        try:
            payload = ReceiptPayload.model_validate_json(content)
        except ValidationError:
            # Do not return raw marking codes in error responses/logs.
            raise HTTPException(422, "Некорректный формат состава приёмки") from None
        import asyncio

        return await asyncio.to_thread(store_getter().sync_receipt, payload)
