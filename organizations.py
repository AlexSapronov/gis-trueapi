"""Справочник организаций. Загружается из конфига, не размазывается по коду."""
from __future__ import annotations

from typing import Optional

from config import settings
from models import Organization


class OrganizationRegistry:
    def __init__(self) -> None:
        self._orgs: dict[str, Organization] = {}
        for o in settings.organizations:
            self._orgs[o["inn"]] = Organization(inn=o["inn"], name=o["name"])

    def all(self) -> list[Organization]:
        return list(self._orgs.values())

    def get(self, inn: str) -> Optional[Organization]:
        return self._orgs.get(inn)

    def has(self, inn: str) -> bool:
        return inn in self._orgs

    def inns(self) -> list[str]:
        return list(self._orgs.keys())


registry = OrganizationRegistry()