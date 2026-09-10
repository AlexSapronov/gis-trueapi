"""Pydantic-схемы REST API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ScanRequest(BaseModel):
    code: str = Field(..., description="Строка Data Matrix")
    org_inn: Optional[str] = None


class ScanBatchRequest(BaseModel):
    codes: list[str] = Field(..., description="Список строк Data Matrix")
    org_inn: Optional[str] = None


class BalanceRequest(BaseModel):
    gtin: str = Field(..., description="GTIN-14")


class TokenUpdateRequest(BaseModel):
    inn: str = Field(..., description="ИНН организации")
    uuid: str = Field(..., description="UUID из 123.ps1")
    signature: str = Field(..., description="SIGNATURE из 123.ps1")


class TokenUpdateResponse(BaseModel):
    ok: bool
    inn: str
    message: str