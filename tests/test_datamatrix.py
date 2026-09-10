"""Unit-тесты Data Matrix parser."""
import pytest

from datamatrix import parse, gtin_checksum_valid, GS


# GTIN из ТЗ: 04640638345218 — валидный GTIN-14.
VALID_GTIN = "04640638345218"


class TestGtinChecksum:
    def test_valid_gtin(self):
        assert gtin_checksum_valid("04640638345218") is True

    def test_invalid_length(self):
        assert gtin_checksum_valid("123") is False
        assert gtin_checksum_valid("123456789012345") is False

    def test_non_digit(self):
        assert gtin_checksum_valid("0464063834521X") is False

    def test_bad_checksum(self):
        # меняем последнюю цифру валидного GTIN
        assert gtin_checksum_valid("04640638345219") is False

    def test_empty(self):
        assert gtin_checksum_valid("") is False


class TestParseBasics:
    def test_normal_code(self):
        code = f"01{VALID_GTIN}21ABC123"
        p = parse(code)
        assert p.structure_valid is True
        assert p.gtin == VALID_GTIN
        assert p.serial == "ABC123"
        assert p.normalized_cis == VALID_GTIN + "ABC123"
        assert p.api_cis == code  # с AI-префиксом, без крипто-хвоста

    def test_21_inside_serial(self):
        # serial содержит последовательность "21", но парсер должен взять
        # первый найденный AI 21 и читать serial от его конца без повторного
        # глобального поиска.
        code = f"01{VALID_GTIN}21SE21RIAL21"
        p = parse(code)
        assert p.structure_valid is True
        assert p.serial == "SE21RIAL21"

    def test_91_inside_gtin_ignored(self):
        # "91" встречается где-то в данных, но не как крипто-тег.
        # GTIN не содержит "91", поэтому подставим "91" в serial и убедимся,
        # что has_crypto не засчитался ложно от голого "91".
        code = f"01{VALID_GTIN}21SERIAL91X"
        p = parse(code)
        assert p.structure_valid is True
        assert p.serial == "SERIAL91X"
        assert p.has_crypto is False

    def test_91_inside_serial_no_false_crypto(self):
        code = f"01{VALID_GTIN}21A91B"
        p = parse(code)
        assert p.serial == "A91B"
        assert p.has_crypto is False

    def test_long_serial(self):
        serial = "S" * 200
        code = f"01{VALID_GTIN}21{serial}"
        p = parse(code)
        assert p.structure_valid is True
        assert p.serial == serial

    def test_double_quote_in_serial(self):
        code = f'01{VALID_GTIN}21AB"CD'
        p = parse(code)
        assert p.serial == 'AB"CD'

    def test_single_quote_in_serial(self):
        code = f"01{VALID_GTIN}21AB'CD"
        p = parse(code)
        assert p.serial == "AB'CD"

    def test_special_chars_in_serial(self):
        serial = '*_-()?><"\''
        code = f"01{VALID_GTIN}21{serial}"
        p = parse(code)
        assert p.serial == serial

    def test_gs_separator(self):
        code = f"01{VALID_GTIN}21SERIAL{GS}91EE06"
        p = parse(code)
        assert p.structure_valid is True
        assert p.serial == "SERIAL"
        assert p.has_crypto is True
        assert p.api_cis == f"01{VALID_GTIN}21SERIAL"  # крипто-хвост отрезан

    def test_91ee_fallback_no_gs(self):
        # сканер не передал GS, но есть "91EE"
        code = f"01{VALID_GTIN}21SERIAL91EE06"
        p = parse(code)
        assert p.serial == "SERIAL"
        assert p.has_crypto is True

    def test_gs_separator_crypto_91(self):
        code = f"01{VALID_GTIN}21SERIAL{GS}91VM1D"
        p = parse(code)
        assert p.structure_valid is True
        assert p.has_crypto is True

    def test_missing_ai21(self):
        code = f"01{VALID_GTIN}"
        p = parse(code)
        assert p.structure_valid is False
        assert p.structure_error is not None

    def test_missing_serial(self):
        code = f"01{VALID_GTIN}21"
        p = parse(code)
        assert p.structure_valid is False
        assert p.structure_error is not None
        assert p.serial == "" or p.serial is None or p.serial == ""

    def test_garbage_string(self):
        code = "hello world !!!"
        p = parse(code)
        assert p.structure_valid is False

    def test_empty_string(self):
        p = parse("")
        assert p.structure_valid is False

    def test_none_input(self):
        p = parse(None)  # type: ignore
        assert p.structure_valid is False

    def test_crlf_stripped(self):
        code = f"01{VALID_GTIN}21SERIAL\r\n"
        p = parse(code)
        assert p.structure_valid is True
        assert p.serial == "SERIAL"

    def test_gs_placeholder_brace(self):
        # некоторые сканеры передают "{GS}" как текст
        code = f"01{VALID_GTIN}21SERIAL{{GS}}91EE06"
        p = parse(code)
        assert p.serial == "SERIAL"
        assert p.has_crypto is True


class TestParseUnambiguous:
    def test_tz_gtin_unambiguous(self):
        # GTIN из ТЗ должен однозначно разложиться на 01 + GTIN + 21 + serial,
        # даже если "21" встречается далее в serial.
        code = "010464063834521821" + "SERIAL21MORE"
        p = parse(code)
        assert p.gtin == "04640638345218"
        assert p.serial == "SERIAL21MORE"
        assert p.structure_valid is True


class TestSerialNotTruncated:
    """Regression: без GS нельзя объявлять любую последовательность цифр
    (91xx/92x/240/3103/93) следующей AI-границей — это ломает serial."""

    def test_serial_91_like_sequence(self):
        p = parse(f"01{VALID_GTIN}21ABC91ABDEF")
        assert p.serial == "ABC91ABDEF"

    def test_serial_240_sequence(self):
        p = parse(f"01{VALID_GTIN}21TEST240ABC")
        assert p.serial == "TEST240ABC"

    def test_serial_93_sequence(self):
        p = parse(f"01{VALID_GTIN}21SERIAL93XYZ")
        assert p.serial == "SERIAL93XYZ"

    def test_serial_3103_sequence(self):
        p = parse(f"01{VALID_GTIN}21A3103TEST")
        assert p.serial == "A3103TEST"

    def test_serial_91ee_still_splits(self):
        # "91EE" — единственный fallback-маркер криптохвоста без GS
        p = parse(f"01{VALID_GTIN}21SERIAL91EE06")
        assert p.serial == "SERIAL"
        assert p.has_crypto is True


class TestScannerPrefix:
    """Regression: scanner prefix должен нормализоваться и не попадать в api_cis."""

    def test_aim_prefix_d2(self):
        code = "]d2" + f"01{VALID_GTIN}21SERIAL"
        p = parse(code)
        assert p.gtin == VALID_GTIN
        assert p.serial == "SERIAL"
        # api_cis начинается непосредственно с "01", без prefix
        assert p.api_cis == f"01{VALID_GTIN}21SERIAL"
        assert p.api_cis.startswith("01")

    def test_aim_prefix_c1(self):
        code = "]C1" + f"01{VALID_GTIN}21SERIAL"
        p = parse(code)
        assert p.gtin == VALID_GTIN
        assert p.serial == "SERIAL"
        assert p.api_cis == f"01{VALID_GTIN}21SERIAL"

    def test_caret_bracket_prefix(self):
        code = "^]" + f"01{VALID_GTIN}21SERIAL"
        p = parse(code)
        assert p.gtin == VALID_GTIN
        assert p.serial == "SERIAL"
        assert p.api_cis == f"01{VALID_GTIN}21SERIAL"