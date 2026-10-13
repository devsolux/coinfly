"""Provider credentials cannot come from ambient environment or model arguments."""

from typing import Protocol

from coinfly.domain.context import DomainError


class ModelProviderGateway(Protocol):
    def request(self, *, model_id: str, prompt: str, deadline_seconds: int) -> str: ...


def create_model_provider_gateway() -> ModelProviderGateway:
    # Requires native MODEL_PROVIDER-purpose enrollment + certified provider
    # origin/egress integration. Never construct a SDK that reads ambient keys.
    raise DomainError("MODEL_PROVIDER_GATEWAY_UNCERTIFIED")
