"""Pydantic-схемы REST API."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Рабочая граница пакетной проверки и разумные лимиты длины входных данных.
MAX_BATCH_SIZE = 1000
MAX_CODE_LENGTH = 512          # один Data Matrix (serial не ограничен 20, но
                               # нужно защититься от мусора/гигантских строк)


def _validate_gtin(value: str) -> str:
    value = (value or "").strip()
    if not value.isdigit() or len(value) != 14:
        raise ValueError("GTIN должен состоять ровно из 14 цифр")
    return value


class ScanRequest(BaseModel):
    code: str = Field(..., description="Строка Data Matrix")
    org_inn: Optional[str] = None

    @field_validator("code")
    @classmethod
    def _code_len(cls, v: str) -> str:
        if len(v) > MAX_CODE_LENGTH:
            raise ValueError(f"Слишком длинный код (максимум {MAX_CODE_LENGTH} символов)")
        return v


class ScanBatchRequest(BaseModel):
    codes: list[str] = Field(..., description="Список строк Data Matrix (максимум 1000)")
    org_inn: Optional[str] = None

    @field_validator("codes")
    @classmethod
    def _codes_size(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_BATCH_SIZE:
            raise ValueError(f"Слишком много кодов: максимум {MAX_BATCH_SIZE}")
        for c in v:
            if len(c) > MAX_CODE_LENGTH:
                raise ValueError(f"Слишком длинный код (максимум {MAX_CODE_LENGTH} символов)")
        return v


class BalanceRequest(BaseModel):
    gtin: str = Field(..., description="GTIN-14")

    @field_validator("gtin")
    @classmethod
    def _gtin_valid(cls, v: str) -> str:
        return _validate_gtin(v)


class TokenUpdateRequest(BaseModel):
    inn: str = Field(..., description="ИНН организации")
    uuid: str = Field(..., description="UUID из refresh_trueapi_token.ps1")
    signature: str = Field(..., description="SIGNATURE из refresh_trueapi_token.ps1")


class TokenUpdateResponse(BaseModel):
    ok: bool
    inn: str
    message: str