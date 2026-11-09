"""Radio templates 36/37; preserve the crypto tail and control bytes exactly."""

import re
from dataclasses import dataclass

GS = "\x1d"


@dataclass(frozen=True)
class Code:
    full: str
    gtin: str
    cis: str
    quantity: int | None


def parse(raw: str) -> Code:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 1024:
        raise ValueError("Пустой или слишком длинный КМ")
    s = raw
    if not re.match(r"^01[0-9]{14}21", s):
        raise ValueError(
            "Ожидается полный КМ радиоэлектроники: 01 + GTIN + 21 + serial"
        )
    gtin = s[2:16]
    check = (
        10
        - sum(int(x) * (3 if i % 2 == 0 else 1) for i, x in enumerate(gtin[:13])) % 10
    ) % 10
    if int(gtin[-1]) != check:
        raise ValueError("Неверная контрольная цифра GTIN")
    # Radio serial is 20 chars including the country prefix. Do not search for
    # 91/92/93 inside it: they can be legitimate serial characters.
    cis = s[:38]
    if len(cis) != 38 or any(ord(c) < 33 or ord(c) > 126 for c in s[18:38]):
        raise ValueError("Ожидается serial из 20 печатных ASCII-символов")
    tail = s[38:]
    if tail.startswith(GS):
        tail = tail[1:]
    if tail.startswith("91"):
        # Template 36: 91(4) + GS + 92(44); restore only fixed boundaries.
        key = tail[2:6]
        rest = tail[6:]
        if rest.startswith(GS):
            rest = rest[1:]
        if len(key) != 4 or not rest.startswith("92") or len(rest[2:]) < 44:
            raise ValueError("Неполный криптохвост 91/92; проверьте сканер")
        crypto, remainder = rest[2:46], rest[46:]
        base = cis + GS + "91" + key + GS + "92" + crypto
    elif tail.startswith("93") and len(tail) >= 6:
        base, remainder = cis + GS + tail[:6], tail[6:]
    else:
        raise ValueError("Нужен полный КМ с криптохвостом 91/92 или 93")
    quantity = None
    if remainder:
        # Keyboard-wedge scanners can omit GS bytes. Crypto lengths above are
        # fixed, so AI 30 starts at an unambiguous boundary after the crypto.
        if remainder.startswith("30"):
            remainder = GS + remainder
        match = re.fullmatch(GS + r"30([0-9]{1,8})", remainder)
        if not match or not 1 <= int(match[1]) <= 99999999:
            raise ValueError(
                "Поддерживается только переменная характеристика количества AI 30 (шт.)"
            )
        quantity = int(match[1])
    if any(ord(c) < 32 and c != GS for c in base):
        raise ValueError("Недопустимый управляющий символ в КМ")
    if base + remainder != raw:
        raise ValueError("СУЗ вернула КМ без обязательных разделителей GS")
    return Code(raw, gtin, cis, quantity)
