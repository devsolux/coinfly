"""No wallet created, loaded, funded or inherited by the model runtime."""

from coinfly.domain.context import DomainError


class NonSigningProvider:
    def get_address(self) -> str:
        raise DomainError("AGENT_WALLET_UNAVAILABLE")

    def sign_message(self, _message: bytes) -> bytes:
        raise DomainError("AGENT_SIGNING_FORBIDDEN")

    def sign_transaction(self, _transaction: bytes) -> bytes:
        raise DomainError("AGENT_SIGNING_FORBIDDEN")

    def send_transaction(self, _transaction: bytes) -> str:
        raise DomainError("AGENT_MUTATION_FORBIDDEN")

    def transfer(self, _destination: str, _quantity: str) -> str:
        raise DomainError("AGENT_MUTATION_FORBIDDEN")


def default_wallet_provider() -> None:
    raise DomainError("DEFAULT_AGENTKIT_WALLET_FORBIDDEN")
