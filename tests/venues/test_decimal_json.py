"""Venue JSON never passes through a binary float: exact decoding, float-free encoding."""

from __future__ import annotations

from decimal import Decimal
from unittest import TestCase

from coinfly.domain.context import DomainError
from coinfly.venues import decimal_json


class DecimalJsonTests(TestCase):
    def test_numbers_decode_exactly_and_strings_stay_strings(self) -> None:
        data = decimal_json.loads('{"price": 0.1, "size": 1e-8, "count": 3, "text": "0.1", "nested": [2.5, {"x": 10.000}]}')
        self.assertEqual(data["price"], Decimal("0.1"))
        self.assertIsInstance(data["price"], Decimal)
        self.assertEqual(data["size"], Decimal("1E-8"))
        self.assertEqual(data["count"], 3)
        self.assertIsInstance(data["count"], int)
        self.assertEqual(data["text"], "0.1")
        self.assertEqual(data["nested"][1]["x"], Decimal("10.000"))  # trailing zeros preserved, as sent
        self.assertNotEqual(str(data["price"]), str(0.1 + 0.2 - 0.2))  # no binary rounding in sight

    def test_non_finite_constants_and_invalid_text_are_refused(self) -> None:
        for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            with self.assertRaisesRegex(DomainError, "JSON_NON_FINITE_NUMBER"):
                decimal_json.loads(text)
        with self.assertRaisesRegex(DomainError, "JSON_INVALID"):
            decimal_json.loads("not json")

    def test_encoding_refuses_floats_and_writes_decimals_exactly(self) -> None:
        document = {"b": Decimal("0.10"), "a": [1, Decimal("1E-8"), "s", True, None], "c": {"z": Decimal("25")}}
        self.assertEqual(decimal_json.dumps(document),
                         '{"a":[1,"0.00000001","s",true,null],"b":"0.10","c":{"z":"25"}}')
        self.assertEqual(decimal_json.dumps(document, decimals="number"),
                         '{"a":[1,0.00000001,"s",true,null],"b":0.10,"c":{"z":25}}')
        with self.assertRaisesRegex(DomainError, "JSON_FLOAT_FORBIDDEN"):
            decimal_json.dumps({"price": 0.1})
        with self.assertRaisesRegex(DomainError, "JSON_NON_FINITE_NUMBER"):
            decimal_json.dumps({"price": Decimal("NaN")})
        with self.assertRaisesRegex(DomainError, "JSON_TYPE_UNSUPPORTED"):
            decimal_json.dumps({"when": object()})
        # Round trip through the number form is exact.
        self.assertEqual(decimal_json.loads(decimal_json.dumps(document, decimals="number"))["b"], Decimal("0.10"))
