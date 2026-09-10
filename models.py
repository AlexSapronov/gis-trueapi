"""Модели данных: организации, статусы, ошибки, результаты скана."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Organization:
    inn: str
    name: str

    def as_dict(self) -> dict[str, str]:
        return {"inn": self.inn, "name": self.name}


# Русский маппинг статусов. Не ограничивает множество — неизвестные статусы
# просто показываются как есть.
STATUS_RU: dict[str, str] = {
    "EMITTED": "Эмитирован",
    "APPLIED": "Нанесён",
    "INTRODUCED": "В обороте",
}


def status_ru(status: Optional[str]) -> str:
    if not status:
        return ""
    return STATUS_RU.get(status, status)


# Категории ошибок, которые различает приложение. Каждая имеет понятное
# русское сообщение для пользователя и технический код для логов.
class ErrorCategory:
    DM_STRUCTURE = "dm_structure"          # неправильная структура Data Matrix
    GTIN_CHECKSUM = "gtin_checksum"        # неверная контрольная цифра GTIN
    KM_NOT_FOUND = "km_not_found"          # КМ не найден
    NO_PERMISSION = "no_permission"        # недостаточно прав
    NO_ORG_TOKEN = "no_org_token"          # нет токена организации
    TOKEN_INVALID = "token_invalid"        # токен недействителен или истёк
    UNAUTHORIZED = "unauthorized"          # 401
    FORBIDDEN = "forbidden"                # 403
    NOT_FOUND = "not_found"                # 404
    VALIDATION = "validation"              # 422
    RATE_LIMIT = "rate_limit"              # 429
    API_UNAVAILABLE = "api_unavailable"    # True API временно недоступен (5xx)
    TIMEOUT = "timeout"                    # Timeout
    API_SERVER_ERROR = "api_server_error"  # ошибка сервера True API
    API_ERROR = "api_error"                # общая ошибка API


CATEGORY_MESSAGES: dict[str, str] = {
    ErrorCategory.DM_STRUCTURE: "Неправильная структура Data Matrix",
    ErrorCategory.GTIN_CHECKSUM: "Неверная контрольная цифра GTIN",
    ErrorCategory.KM_NOT_FOUND: "КМ не найден",
    ErrorCategory.NO_PERMISSION: "Недостаточно прав для операции",
    ErrorCategory.NO_ORG_TOKEN: "Нет токена организации",
    ErrorCategory.TOKEN_INVALID: "Токен недействителен или истёк",
    ErrorCategory.UNAUTHORIZED: "Ошибка авторизации (401)",
    ErrorCategory.FORBIDDEN: "Доступ запрещён (403)",
    ErrorCategory.NOT_FOUND: "Ресурс не найден (404)",
    ErrorCategory.VALIDATION: "Ошибка валидации запроса (422)",
    ErrorCategory.RATE_LIMIT: "Превышен лимит запросов, попробуйте позже",
    ErrorCategory.API_UNAVAILABLE: "Сервис маркировки временно недоступен",
    ErrorCategory.TIMEOUT: "Превышено время ожидания ответа сервиса",
    ErrorCategory.API_SERVER_ERROR: "Ошибка сервера маркировки",
    ErrorCategory.API_ERROR: "Ошибка сервиса маркировки",
}


@dataclass
class AppError(Exception):
    category: str
    message: str
    technical: str = ""

    def __str__(self) -> str:
        return self.message


@dataclass
class ScanResult:
    """Результат проверки одного КМ."""
    raw_code: str = ""
    gtin: Optional[str] = None
    serial: Optional[str] = None
    normalized_cis: Optional[str] = None

    # локальная проверка
    structure_valid: bool = True
    structure_error: Optional[str] = None

    # данные из True API
    product_name: Optional[str] = None
    status: Optional[str] = None
    status_ex: Optional[str] = None
    owner_inn: Optional[str] = None
    owner_name: Optional[str] = None
    producer_inn: Optional[str] = None
    package_type: Optional[str] = None
    quantity_in_pack: Optional[int] = None

    # интерпретация
    ours: Optional[bool] = None
    our_org_name: Optional[str] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None

    # сырые технические данные (не обязательны, для отладки/раскрытия)
    raw_api: Optional[dict[str, Any]] = None
    api_checked: bool = False

    def as_dict(self, include_raw_api: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "raw_code": self.raw_code,
            "gtin": self.gtin,
            "serial": self.serial,
            "normalized_cis": self.normalized_cis,
            "structure_valid": self.structure_valid,
            "structure_error": self.structure_error,
            "product_name": self.product_name,
            "status": self.status,
            "status_ex": self.status_ex,
            "owner_inn": self.owner_inn,
            "owner_name": self.owner_name,
            "producer_inn": self.producer_inn,
            "package_type": self.package_type,
            "quantity_in_pack": self.quantity_in_pack,
            "ours": self.ours,
            "our_org_name": self.our_org_name,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "api_checked": self.api_checked,
        }
        # raw_api не отдаём в обычный REST-ответ (UI его не использует, а там
        # сырые данные ЧЗ). Включаем только в явном debug-режиме для диагностики.
        if include_raw_api:
            d["raw_api"] = self.raw_api
        return d