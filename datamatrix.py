"""Парсер Data Matrix кодов маркировки «Честный ЗНАК».

Реализует GS1-aware разбор с поддержкой FNC1/GS (`\x1d`) и отдельных
fallback-правил для сканеров, которые не передают нормальный GS.

Ключевые принципы:
- GTIN ищем структурно: 01 + ровно 14 цифр + 21. Serial начинается сразу
  после конца найденного `21` (позиция match.end()), а НЕ новым глобальным
  поиском `21` по всей строке.
- Криптографическую часть (AI 91/92) не ищем по голому `91`/`92` — эти
  последовательности могут встретиться внутри GTIN или serial. Работаем
  через GS (`\x1d`) либо единственный надёжный scanner fallback `91EE`.
- GTIN проверяем по контрольной цифре.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# GS (Group Separator) = FNC1 в GS1 Data Matrix
GS = "\x1d"

# Physical Windows keyboard keys, including Shift punctuation. Convert a whole
# scan only when Russian layout is detectable; valid ASCII codes stay unchanged.
_RU_KEYS = 'ёйцукенгшщзхъфывапролджэячсмитьбю'
_EN_KEYS = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
_EN_SHIFT_KEYS = '~QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>'
_RU_TO_EN = str.maketrans({
    **dict(zip(_RU_KEYS, _EN_KEYS)),
    **dict(zip(_RU_KEYS.upper(), _EN_SHIFT_KEYS)),
    '"': '@', '№': '#', ';': '$', ':': '^', '?': '&', '.': '/', ',': '?', '/': '|',
})


def normalize_scanner_layout(raw: str) -> str:
    """Restore English keys from a scan entered with the Russian layout."""
    return raw.translate(_RU_TO_EN) if re.search(r'[А-Яа-яЁё№]', raw) else raw

# AI 01 + 14 цифр + AI 21. Serial начинается сразу после match.end().
_PATTERN_01_21 = re.compile(r"01(\d{14})21")

# Barcode-кодирование криптохвоста ЧЗ: AI 91 c прикладным значением "EE".
# В GS-потоке разделитель — \x1d; без GS единственный надёжный маркер
# криптохвоста — литерал "91EE".
_PATTERN_91EE = re.compile(r"91EE")
_PATTERN_91 = re.compile(r"91([0-9A-Fa-f]{4})")
_PATTERN_92 = re.compile(r"92([0-9A-Fa-f]{4})")

# Возможные symbology prefixes от сканеров (AIM / коммерческие), которые
# могут оказаться перед информацией GS1. Нормализуем их до разбора, чтобы
# они не попадали в GTIN/serial/api_cis.
_SCANNER_PREFIXES = (
    "]d2",   # Data Matrix ECC200
    "]C1",   # GS1 DataMatrix (AIM)
    "]e0",   # GS1 DataMatrix (AIM, альтернативный)
    "^]",    # ASCII GS-замена (FNC1)
)


def _normalize_scanner_prefix(s: str) -> str:
    for p in _SCANNER_PREFIXES:
        if s.startswith(p):
            return s[len(p):]
    return s


# Scanner firmware and Excel files may represent FNC1 / Group Separator as
# readable text rather than the actual ASCII 29 control character.
_GS_ALIASES = re.compile(r"(?i)<(?:GS|FNC1?)>|\{(?:GS|FNC1?)\}|\[(?:GS|FNC1?)\]")

# Certain radio labels have a fixed 20-character serial and fixed crypto fields.
# Rebuild their separators even if the scanner discarded some or all of them.
# Do not infer AI boundaries from arbitrary "30", "91", "92" in a serial.
_RADIO_91_92 = re.compile(
    r'\A(01[0-9]{14}21[!-~]{20})\x1d?(91[!-~]{4})\x1d?(92[!-~]{44})(?:\x1d?(30[0-9]{1,8}))?\Z'
)
_RADIO_93 = re.compile(
    r'\A(01[0-9]{14}21[!-~]{20})\x1d?(93[!-~]{4})(?:\x1d?(30[0-9]{1,8}))?\Z'
)


def canonical_full(raw: str) -> str:
    """Decode known scanner prefixes and separator representations.

    An initial FNC1 marks the symbology; it is not part of the KM payload.
    Other FNC1/GS markers are represented as ASCII 29 separators. Original
    input stays in raw_code so exports do not silently rewrite source data.
    """
    s = normalize_scanner_layout(raw).strip(' \r\n')
    s = _normalize_scanner_prefix(s)
    s = _GS_ALIASES.sub(GS, s).replace('^]', GS).replace('\r', '').replace('\n', '')
    return s.lstrip(GS)


def restore_radio_separators(full: str) -> str:
    """Reconstruct only unambiguous separator boundaries of radio KMs."""
    for pattern in (_RADIO_91_92, _RADIO_93):
        match = pattern.fullmatch(full)
        if match:
            return GS.join(group for group in match.groups() if group is not None)
    return full


def strip_ai30(full: str) -> str:
    """Remove an optional *trailing* AI 30 without altering crypto contents.

    An AI 30 with missing GS is handled for fixed-format radio codes by the
    separator restorer. For ambiguous formats we never delete possible crypto.
    """
    full = restore_radio_separators(canonical_full(full))
    return re.sub(r'\x1d30[0-9]{1,8}\Z', '', full)


def full_code_for_check(raw: str) -> str:
    """Produce the full KM for crypto validation (True API cises/check).

    Unlike cises/info, cises/check needs the cryptographic tail and its real
    ASCII 29 separators. Reconstruct only boundaries that are recognizable.
    """
    full = strip_ai30(raw)
    parsed = parse(full)
    if parsed.structure_valid and parsed.tail and not parsed.tail.startswith(GS):
        return f'01{parsed.gtin}21{parsed.serial}{GS}{parsed.tail}'
    return full



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
    # код для отправки в True API cises/info: собирается из компонентов
    # "01" + gtin + "21" + serial (без scanner prefix и без крипто-части).
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

    Serial продолжается до первого GS (`\x1d`). Без GS (scanner fallback)
    единственная надёжная граница — литерал `91EE` (криптохвост). AI 21 имеет
    переменную длину, поэтому произвольную последовательность цифр (91xx, 92x,
    240, 3103, 93) НЕ считаем AI-границей — она может быть легальной частью
    serial. Возвращает (serial, tail).
    """
    rest = full[pos:]
    # Если есть GS — serial до первого GS.
    if GS in rest:
        serial, tail = rest.split(GS, 1)
        return serial, GS + tail

    # Без GS: заведомо известный scanner-normalized криптохвост — только "91EE".
    m = _PATTERN_91EE.search(rest)
    if m:
        idx = m.start()
        serial, tail = rest[:idx], rest[idx:]
        return serial, tail

    return rest, ""


def parse(raw: str) -> ParsedCode:
    """Разбор одного Data Matrix кода. Возвращает ParsedCode."""
    if not isinstance(raw, str):
        raw = ""

    result = ParsedCode(raw_code=raw)
    s = restore_radio_separators(canonical_full(raw))

    if not s:
        result.structure_valid = False
        result.structure_error = "Пустой код"
        return result


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

    # Криптографическая часть: в tail у нас GS-поток или "91EE..." fallback.
    has_crypto = False
    if tail:
        if _PATTERN_91.search(tail) or _PATTERN_92.search(tail) or "91EE" in tail:
            has_crypto = True
        if tail.startswith(GS) and ("91" in tail or "92" in tail):
            has_crypto = True
        # Radio template 37: a single fixed four-character AI 93 crypto key.
        if tail.startswith(GS + "93") and len(tail) >= 7:
            has_crypto = True

    result.has_crypto = has_crypto
    # normalized_cis = GTIN + serial (без крипто-хвоста).
    result.normalized_cis = gtin + serial
    # api_cis собирается ИЗ КОМПОНЕНТОВ — никакой scanner prefix не попадает.
    result.api_cis = "01" + gtin + "21" + serial
    result.structure_valid = True
    result.structure_error = None
    return result


def parse_or_error(raw: str) -> ParsedCode:
    return parse(raw)
