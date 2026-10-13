"""Decimal-exact JSON for venue payloads.

Advanced Trade documents numerics as strings; the Global Derivatives gateway
(JSON-RPC 2.0) carries JSON numbers. A binary float never represents a price
or a quantity here: decoding turns every JSON number with a fraction or an
exponent into ``Decimal`` (integers stay ``int``), and encoding refuses
``float`` outright and writes ``Decimal`` either as a string (Advanced Trade)
or as an exact JSON number literal (Global Derivatives) — never through a
float conversion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from coinfly.domain.context import DomainError


def _reject_constant(name: str) -> Any:
    raise DomainError("JSON_NON_FINITE_NUMBER", f"JSON_NON_FINITE_NUMBER: {name}")


def loads(text: str | bytes) -> Any:
    """Parse venue JSON with exact numbers: fractions and exponents become Decimal, NaN/Infinity are refused."""
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except DomainError:
        raise
    except (ValueError, TypeError):
        raise DomainError("JSON_INVALID") from None


def dumps(document: Any, *, decimals: Literal["string", "number"] = "string") -> str:
    """Serialize a venue request body; floats are refused, Decimals are exact.

    ``decimals="string"`` writes ``Decimal("0.1")`` as ``"0.1"`` (Advanced Trade);
    ``decimals="number"`` writes it as the JSON number literal ``0.1`` (JSON-RPC
    gateways that expect numbers). Keys are sorted and the output is compact so
    the encoding is canonical for digests.
    """
    return _encode(document, decimals)


def _encode(value: Any, decimals: str) -> str:
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, float):
        raise DomainError("JSON_FLOAT_FORBIDDEN")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DomainError("JSON_NON_FINITE_NUMBER")
        literal = format(value, "f")
        return json.dumps(literal) if decimals == "string" else literal
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise DomainError("JSON_KEY_INVALID")
        return "{" + ",".join(f"{json.dumps(key, ensure_ascii=False)}:{_encode(item, decimals)}"
                              for key, item in sorted(value.items())) + "}"
    if isinstance(value, Sequence):
        return "[" + ",".join(_encode(item, decimals) for item in value) + "]"
    raise DomainError("JSON_TYPE_UNSUPPORTED", f"JSON_TYPE_UNSUPPORTED: {type(value).__name__}")
