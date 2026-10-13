"""Strict operator-config decoding. Broker attestations are never accepted here."""

import json
from typing import Any

from pydantic import ValidationError

from coinfly.config.models import EngineConfig
from coinfly.domain.context import DomainError

MAX_CONFIG_BYTES = 16_384


def decode_operator_config(value: object) -> EngineConfig:
    if not isinstance(value, dict):
        raise DomainError("INVALID_CONFIG")
    try:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise DomainError("CONFIG_TOO_LARGE")
        return EngineConfig.model_validate_json(raw)
    except (ValidationError, TypeError, ValueError):
        # Pydantic errors(include_input=True) can echo secrets submitted to forbidden fields.
        raise DomainError("INVALID_CONFIG", "Configuration failed strict validation") from None


def public_config(config: EngineConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")
