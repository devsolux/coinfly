"""A bounded monetary input contract and deterministic arithmetic context."""

from collections.abc import Callable
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from functools import wraps
from typing import Annotated, Any, ParamSpec, TypeVar

from pydantic import BeforeValidator, Field, ValidationInfo


def _exact_input(value: Any, info: ValidationInfo) -> Any:
    # Pydantic's strict JSON Decimal path otherwise accepts a binary double,
    # silently losing digits before Decimal validation can inspect the amount.
    if isinstance(value, (float, bool)):
        raise ValueError("Decimal amounts require exact strings or integers, never binary floats")
    if info.mode == "json" and isinstance(value, (str, int)):
        try:
            return Decimal(value)
        except InvalidOperation:
            raise ValueError("Invalid exact Decimal amount") from None
    return value


ExactDecimal = Annotated[Decimal, BeforeValidator(_exact_input)]
D = Annotated[ExactDecimal, Field(max_digits=38, decimal_places=18, allow_inf_nan=False)]
P = ParamSpec("P")
R = TypeVar("R")


def financial_context(precision: int = 100) -> Context:
    """Fresh context independent of ambient settings and mutable DefaultContext."""
    return Context(prec=precision, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
                   capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow])


def exact_decimal(function: Callable[P, R]) -> Callable[P, R]:
    """Isolate bounded financial arithmetic from ambient thread decimal settings.

    Division can still require rounding; quantity rounding must separately use
    venue increments and ROUND_DOWN. This context supplies deterministic precision.
    """
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with localcontext(financial_context()):
            return function(*args, **kwargs)
    return wrapped
