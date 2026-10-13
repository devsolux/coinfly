"""Native model isolation is a release prerequisite, never a Python sandbox."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from coinfly.domain.context import ContextKey, DomainError


@dataclass(frozen=True, slots=True)
class ModelRun:
    run_id: str
    context: ContextKey
    deadline: datetime
    max_tool_calls: int = 32


class NativeIsolatedModelHost(Protocol):
    """Signed, OS-sandboxed fresh-spawn host with scoped dataset and IPC access.

    Implementations require native filesystem, handle and egress restrictions.
    A multiprocessing process alone does not provide those restrictions.
    """

    def run(self, request: ModelRun) -> None: ...
    def retire_context(self, context: ContextKey) -> None: ...


def create_model_host() -> NativeIsolatedModelHost:
    # There is no certified restricted native helper supplied in this checkout.
    # No subprocess/AgentKit default construction is used as a silent fallback.
    raise DomainError("MODEL_NATIVE_ISOLATION_UNCERTIFIED")
