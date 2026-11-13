"""Scoped marking-bm ingress. One durable source identity per receipt."""

from __future__ import annotations

import hashlib
import hmac
import json
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


class RevokePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_instance: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    source_document: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    revision: int = Field(strict=True, ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    action: str = Field(pattern=r"^revoke$")
    receipt: ReceiptPayload | None = None


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
        c.execute("""CREATE TABLE IF NOT EXISTS warehouse_receipt_revocations (
            source_instance TEXT NOT NULL, source_document TEXT NOT NULL,
            revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, document_id TEXT,
            PRIMARY KEY(source_instance,source_document))""")
        c.execute(
            "CREATE INDEX IF NOT EXISTS receipt_bridge_expected_cis ON warehouse_expected(cis,document_id)"
        )

    def _match_existing_receipt(self, c, payload, lines, expected):
        """Adopt only a single complete match; never overwrite an imported receipt."""
        from warehouse_service import _receipt_full_code_error
        from warehouse_store import WarehouseError

        candidates = list(
            c.execute(
                "SELECT DISTINCT d.* FROM warehouse_documents d WHERE d.kind='receipt' AND NOT EXISTS (SELECT 1 FROM warehouse_receipt_revocations v WHERE v.document_id=d.id) AND "
                "((d.org_inn=? AND trim(d.number)=?) OR d.id IN "
                "(SELECT document_id FROM warehouse_expected WHERE cis IN (SELECT value FROM json_each(?))))",
                (
                    payload.org_inn,
                    payload.number.strip(),
                    json.dumps([e["cis"] for e in expected]),
                ),
            )
        )
        if not candidates:
            return None
        message = "Существующая приёмка не совпадает с накладной или найдены несколько кандидатов. Нужен разбор администратора"

        def conflict():
            raise WarehouseError(message, 409, "receipt_match_conflict")

        if len(candidates) != 1:
            conflict()
        d = candidates[0]
        if d["org_inn"] != payload.org_inn:
            conflict()
        if c.execute(
            "SELECT 1 FROM warehouse_receipt_sources WHERE document_id=?", (d["id"],)
        ).fetchone():
            conflict()
        original = {
            e["cis"]: dict(e)
            for e in c.execute(
                "SELECT * FROM warehouse_expected WHERE document_id=?", (d["id"],)
            )
        }
        if set(original) != {e["cis"] for e in expected}:
            conflict()
        if any(
            original[e["cis"]]["quantity"] != e["quantity"]
            or _receipt_full_code_error(
                original[e["cis"]]["full_code"], e["full_code"], e["quantity"]
            )
            for e in expected
        ):
            conflict()
        actual_lines = {
            r["gtin"]: (r["pn"].strip(), r["quantity"])
            for r in c.execute(
                "SELECT * FROM warehouse_lines WHERE document_id=?", (d["id"],)
            )
        }
        if actual_lines != {r["gtin"]: (r["pn"], r["quantity"]) for r in lines}:
            conflict()
        return d["id"]

    def _replace_receipt_work(self, c, document_id, action, details, *, revoked=False):
        """Retire slave work atomically; master revisions always take precedence."""
        from warehouse_store import now

        before = self._doc(c, document_id, include_deleted=True)
        snapshot = {"document": before, **details}
        for key, table in (("lines", "warehouse_lines"),
                           ("unmarked_lines", "warehouse_unmarked_lines"),
                           ("expected", "warehouse_expected"),
                           ("scans", "warehouse_scans")):
            snapshot[key] = [dict(r) for r in c.execute(
                f"SELECT * FROM {table} WHERE document_id=?", (document_id,))]
        self._event(c, document_id, action, snapshot, actor="marking-bm")
        for table in ("warehouse_scans", "warehouse_expected", "warehouse_lines", "warehouse_unmarked_lines"):
            c.execute(f"DELETE FROM {table} WHERE document_id=?", (document_id,))
        c.execute("""UPDATE warehouse_documents SET status='draft',started_at=NULL,
            completed_at=NULL,archived_at=NULL,deleted_at=?,check_id=NULL,check_until=NULL,
            result=NULL,result_version=NULL,result_at=NULL WHERE id=?""",
            (now() if revoked else None, document_id))
        c.execute("UPDATE warehouse_issues SET resolved_at=?,resolution=? WHERE document_id=? AND resolved_at IS NULL",
                  (now(), "Прежняя версия отозвана источником marking-bm", document_id))
        self._bump(c, document_id)

    def revoke_receipt(self, payload):
        from warehouse_store import WarehouseError

        fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        with self.connection(True) as c:
            previous = c.execute("SELECT * FROM warehouse_receipt_revocations WHERE source_instance=? AND source_document=?", (payload.source_instance, payload.source_document)).fetchone()
            source = c.execute("SELECT * FROM warehouse_receipt_sources WHERE source_instance=? AND source_document=?", (payload.source_instance, payload.source_document)).fetchone()
            if previous:
                if payload.revision < previous["revision"] or (payload.revision == previous["revision"] and fingerprint != previous["fingerprint"]):
                    raise WarehouseError("Устаревший или противоречивый отзыв", 409)
                if payload.revision == previous["revision"]:
                    return {"document_id": previous["document_id"] or payload.source_document, "revision": payload.revision, "status": "revoked"}
            if not source and payload.receipt:
                original = payload.receipt
                if (original.source_instance != payload.source_instance
                    or original.source_document != payload.source_document
                    or original.revision >= payload.revision):
                    raise WarehouseError("Исходная приёмка не соответствует отзыву", 409)
                _, lines, expected = prepare(original)
                matched = self._match_existing_receipt(c, original, lines, expected)
                if matched:
                    metadata = json.dumps({"supplier": original.supplier, "atks": sorted({i.atk for i in original.codes}), "source_document": original.source_document}, ensure_ascii=False)
                    c.execute("INSERT INTO warehouse_receipt_sources VALUES(?,?,?,?,?,?)", (original.source_instance, original.source_document, matched, original.revision, hashlib.sha256(original.model_dump_json().encode()).hexdigest(), metadata))
                    source = c.execute("SELECT * FROM warehouse_receipt_sources WHERE source_instance=? AND source_document=?", (payload.source_instance, payload.source_document)).fetchone()
            if source and payload.revision <= source["revision"]:
                raise WarehouseError("Устаревшая версия отзыва", 409)
            document_id = source["document_id"] if source else None
            if source:
                self._replace_receipt_work(c, document_id, "receipt_revoked",
                    {"reason": payload.reason, "revision": payload.revision,
                     "source": json.loads(source["metadata"])}, revoked=True)
            c.execute("""INSERT INTO warehouse_receipt_revocations VALUES(?,?,?,?,?)
                ON CONFLICT(source_instance,source_document) DO UPDATE SET revision=excluded.revision,fingerprint=excluded.fingerprint,document_id=excluded.document_id""",
                (payload.source_instance, payload.source_document, payload.revision, fingerprint, document_id))
        return {"document_id": document_id or payload.source_document, "revision": payload.revision, "status": "revoked"}

    def sync_receipt(self, payload):
        from warehouse_store import WarehouseError, now

        org, lines, expected = prepare(payload)
        encoded = payload.model_dump_json()
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connection(True) as c:
            revoked = c.execute("SELECT * FROM warehouse_receipt_revocations WHERE source_instance=? AND source_document=?", (payload.source_instance, payload.source_document)).fetchone()
            if revoked and payload.revision <= revoked["revision"]:
                raise WarehouseError("Версия состава уже отозвана", 409)
            source = c.execute(
                "SELECT * FROM warehouse_receipt_sources WHERE source_instance=? AND source_document=?",
                (payload.source_instance, payload.source_document),
            ).fetchone()
            if source:
                document_id = source["document_id"]
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
                self._replace_receipt_work(c, document_id, "receipt_superseded",
                    {"revision": payload.revision, "previous_revision": source["revision"],
                     "source": json.loads(source["metadata"])})
                c.execute("""UPDATE warehouse_documents SET number=?,date=?,org_inn=?,org_name=? WHERE id=?""",
                          (payload.number, payload.date.isoformat(), org.inn, org.name, document_id))
                status = "updated"
            else:
                if not expected:
                    raise WarehouseError("Нельзя создать пустую приёмку")
                matched = self._match_existing_receipt(c, payload, lines, expected)
                if matched:
                    metadata = json.dumps(
                        {
                            "supplier": payload.supplier,
                            "atks": sorted({i.atk for i in payload.codes}),
                            "source_document": payload.source_document,
                        },
                        ensure_ascii=False,
                    )
                    c.execute(
                        "INSERT INTO warehouse_receipt_sources VALUES (?,?,?,?,?,?)",
                        (
                            payload.source_instance,
                            payload.source_document,
                            matched,
                            payload.revision,
                            fingerprint,
                            metadata,
                        ),
                    )
                    self._event(
                        c,
                        matched,
                        "receipt_matched",
                        {"revision": payload.revision},
                        actor="marking-bm",
                    )
                    return {
                        "document_id": matched,
                        "revision": payload.revision,
                        "status": "matched",
                    }
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
            c.execute("DELETE FROM warehouse_receipt_revocations WHERE source_instance=? AND source_document=?", (payload.source_instance, payload.source_document))
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
    @app.post("/api/integrations/marking-bm/receipts/revoke")
    async def revoke(request: Request):
        return await handle(request, revoking=True)

    @app.post("/api/integrations/marking-bm/receipts")
    async def receive(request: Request):
        return await handle(request, revoking=False)

    async def handle(request, revoking):
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
            payload = (RevokePayload if revoking else ReceiptPayload).model_validate_json(content)
        except ValidationError:
            # Do not return raw marking codes in error responses/logs.
            raise HTTPException(422, "Некорректный формат состава приёмки") from None
        import asyncio

        return await asyncio.to_thread(store_getter().revoke_receipt if revoking else store_getter().sync_receipt, payload)
