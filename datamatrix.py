"""Парсер Data Matrix кодов маркировки «Честный ЗНАК».

Реализует GS1-aware разбор с поддержкой FNC1/GS (`\\x1d`) и отдельных
fallback-правил для сканеров, которые не передают нормальный GS.

Ключевые принципы:
- GTIN ищем структурно: 01 + ровно 14 цифр + 21. Serial начинается сразу
  после конца найденного `21` (позиция match.end()), а НЕ новым глобальным
  поиском `21` по всей строке.
- Криптографическую часть (AI 91/92) не ищем по голому `91`/`92` — эти
  последовательности могут встретиться внутри GTIN или serial. Работаем
  через GS (\\x1d) либо fallback-паттерн `91EE`/`92...` (HEX-коды).
- GTIN проверяем по контрольной цифре.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# GS (Group Separator) = FNC1 в GS1 Data Matrix
GS = "\x1d"

# AI 01 + 14 цифр + AI 21. Serial начинается сразу после match.end().
_PATTERN_01_21 = re.compile(r"01(\d{14})21")

# Крипто.часть: AI 91 (эл.подпись) с hex-значением, AI 92 (код проверки).
# Иском структурно после GS, либо по fallback-паттерну "91EE".
_PATTERN_91 = re.compile(r"91([0-9A-Fa-f]{4})")
_PATTERN_92 = re.compile(r"92([0-9A-Fa-f]{4})")


@dataclass
class ParsedCode:
    raw_code: str
    gtin: Optional[str] = None
    serial: Optional[str] = None
    normalized_cis: Optional[str] = None
    has_crypto: bool = False
    structure_valid: bool = True
    structure_error: Optional[str] = None
    # отрезок строки после serial (например, крипто.часть), для отладки
    tail: Optional[str] = None
    # код для отправки в True API cises/info: исходный вид с AI 01 и 21
    # (например "010464063834521821<SERIAL>"), без крипто-части.
    api_cis: Optional[str] = None


def gtin_checksum_valid(gtin: str) -> bool:
    """Проверка контрольной цифры GTIN-14 (GS1). Последняя цифра — контрольная."""
    if len(gtin) != 14 or not gtin.isdigit():
        return False
    digits = [int(c) for c in gtin]
    # веса справа налево: 3,1,3,1,... начиная с предпоследней цифры
    total = 0
    for i in range(13):
        d = digits[i]
        # позиции с 0-индексом: нечётный индекс (1,3,5...) умножаем на 3
        total += d * 3 if i % 2 == 0 else d
    check = (10 - (total % 10)) % 10
    return check == digits[13]


def _extract_serial(full: str, pos: int) -> tuple[str, str]:
    """После позиции `pos` (конец AI 21) отделяем serial от хвоста.

    Serial продолжается до первого GS (\\x1d) либо, при его отсутствии, до
    распознанного AI-тега (91/240/3103 и т.п.). Возвращает (serial, tail).
    """
    rest = full[pos:]
    # Если есть GS — serial до первого GS.
    if GS in rest:
        serial, tail = rest.split(GS, 1)
        return serial, GS + tail

    # Без GS: ищем границу по началу другого AI. Серийные номера «Честного
    # ЗНАКА» — это префикс "01"+... ASCII-строка; остальные AI начинаются с
    # двух цифр. Остановимся на заведомо известных AI-тегах после serial.
    ai_boundary = re.search(r"(?=91(?:[0-9A-Fa-f]{2}|EE)|92[0-9A-Fa-f]|240|3103|93)", rest)
    if ai_boundary:
        idx = ai_boundary.start()
        serial, tail = rest[:idx], rest[idx:]
        return serial, tail

    return rest, ""


def parse(raw: str) -> ParsedCode:
    """Разбор одного Data Matrix кода. Возвращает ParsedCode."""
    if not isinstance(raw, str):
        raw = ""

    result = ParsedCode(raw_code=raw)
    s = raw.strip()

    if not s:
        result.structure_valid = False
        result.structure_error = "Пустой код"
        return result

    # Нормализация: некоторые сканеры передают замену FNC1 как {GS} или ^].
    # Также пробельные символы в начале/середине (Windows CR) — убираем.
    s = s.replace("{GS}", GS).replace("^]", GS).replace("\r", "").replace("\n", "")

    m = _PATTERN_01_21.search(s)
    if not m:
        result.structure_valid = False
        result.structure_error = "Не найдена структура 01(GTIN)21(serial)"
        return result

    gtin = m.group(1)
    serial_pos = m.end()  # позиция сразу после "21"
    result.gtin = gtin

    if not gtin_checksum_valid(gtin):
        result.gtin = gtin
        result.structure_valid = False
        result.structure_error = "Неверная контрольная цифра GTIN"
        return result

    serial, tail = _extract_serial(s, serial_pos)
    result.serial = serial
    result.tail = tail if tail else None

    if not serial:
        result.structure_valid = False
        result.structure_error = "Не найден serial (после AI 21)"
        return result

    # Криптографическая часть.
    has_crypto = False
    if tail:
        # GS-aware: ищем 91 в tail; но используем только 4-символьный hex
        # (либо fallback "91EE"). Голый "91" игнорируем.
        if _PATTERN_91.search(tail) or _PATTERN_92.search(tail) or "91EE" in tail:
            has_crypto = True
        # Если tail начинается с GS и дальше идёт 91 — тоже крипто.
        if tail.startswith(GS) and ("91" in tail or "92" in tail):
            has_crypto = True

    result.has_crypto = has_crypto
    # normalized_cis = GTIN + serial (без крипто-хвоста), как используется
    # в запросах True API.
    result.normalized_cis = gtin + serial
    # api_cis = полный код с AI для cises/info: "01" + GTIN + "21" + serial,
    # без крипто-части (tail). Позиция конца serial = serial_pos + len(serial).
    result.api_cis = s[: serial_pos + len(serial)]
    result.structure_valid = True
    result.structure_error = None
    return result


def parse_or_error(raw: str) -> ParsedCode:
    return parse(raw)