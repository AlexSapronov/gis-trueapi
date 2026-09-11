"""Конфигурация приложения. Читает .env и env-переменные о кружения."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Дефолтный список организаций (если .env / env не заданы). Служит эталоном и
# позволяет тестам работать без .env.
DEFAULT_ORGANIZATIONS = (
    '0000000001=ООО "ОРГАНИЗАЦИЯ 1";'
    '0000000002=ООО "ОРГАНИЗАЦИЯ 2";'
    '0000000003=ООО "ОРГАНИЗАЦИЯ 3";'
    '0000000004=ООО "ОРГАНИЗАЦИЯ 4"'
)


class Settings:
    def __init__(self) -> None:
        self.mode: str = os.getenv("GIS_MODE", "mock").strip().lower()
        self.base_url: str = os.getenv("GIS_BASE_URL", "https://markirovka.crpt.ru").rstrip("/")
        self.admin_key: str = os.getenv("ADMIN_KEY", "change-me-in-production")
        self.tokens_file: Path = Path(
            os.getenv("TOKENS_FILE", str(BASE_DIR / "data" / "tokens.json"))
        )
        # Товарные группы для поиска (cises/search требует filter.productGroups).
        # Радиоэлектронная продукция = "radio"; при необходимости дополнить через env.
        self.product_groups: list[str] = [
            x.strip()
            for x in os.getenv("PRODUCT_GROUPS", "radio").split(",")
            if x.strip()
        ]
        self.organizations = self._parse_organizations(
            os.getenv("ORGANIZATIONS", DEFAULT_ORGANIZATIONS)
        )

    @staticmethod
    def _parse_organizations(raw: str) -> list[dict[str, str]]:
        """Разбор строки 'INN=Название;INN=Название;...'."""
        result: list[dict[str, str]] = []
        if not raw.strip():
            return result
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" not in chunk:
                continue
            inn, name = chunk.split("=", 1)
            inn = inn.strip()
            name = name.strip()
            # Убираем только парные внешние кавычки вокруг названия (если название
            # в кавычках целиком). Внутренние кавычки в названии не трогаем.
            if len(name) >= 2 and name[0] == name[-1] and name[0] in ('"', "'"):
                name = name[1:-1]
            if inn and name:
                result.append({"inn": inn, "name": name})
        return result

    @property
    def is_mock(self) -> bool:
        return self.mode == "mock"


settings = Settings()