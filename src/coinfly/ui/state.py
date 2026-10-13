"""Pure presentation decisions independent of Tk, SQL, secrets and brokers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from coinfly.ipc.protocol import RPCError, freeze, thaw

IN_PROGRESS_TRANSITION_PHASES = frozenset({
    "REQUESTED", "QUIESCING", "DRAINING", "RECONCILING", "STOPPED_OLD", "RESETTING",
    "NO_PREVIOUS_CONTEXT", "INITIALIZING_NEW",
})
RETRYABLE_TRANSITION_PHASES = frozenset({"BLOCKED_DRAIN", "FAILED_INITIALIZATION"})


def unavailable_snapshot() -> Mapping[str, Any]:
    return cast(Mapping[str, Any], freeze({"sequence": 0, "context_epoch": 0, "config_revision": 0,
                   "effective_config": {}, "proposed_config": {}, "engine_state": "UNAVAILABLE",
                   "bootstrap": {"stage": "CHECKING", "ready": False, "missing_evidence": ["WORKER_UNAVAILABLE"]},
                   "balance": {"coverage": "UNKNOWN_UNIVERSE", "environment": "SANDBOX", "equity": None},
                   "slots": [], "alarms": [], "transition": None, "keystore": "UNVERIFIED"}))


@dataclass(frozen=True, slots=True)
class DesktopState:
    snapshot: Mapping[str, Any] = field(default_factory=unavailable_snapshot)
    connected: bool = False
    local_source_fingerprint: str | None = None

    @property
    def effective(self) -> Mapping[str, Any]:
        value = self.snapshot.get("effective_config") or {}
        return cast(Mapping[str, Any], value.get("config", value))

    @property
    def proposed(self) -> Mapping[str, Any]:
        value = self.snapshot.get("proposed_config") or {}
        return cast(Mapping[str, Any], value.get("config", value))

    @property
    def ready(self) -> bool:
        bootstrap = self.snapshot.get("bootstrap", {})
        return self.connected and bootstrap.get("ready") is True and bootstrap.get("stage") == "READY"

    @property
    def transition(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self.snapshot.get("transition") or {})

    @property
    def transition_phase(self) -> str | None:
        transition = self.transition
        if not transition:
            return None
        phase = transition.get("state", transition.get("phase"))
        return str(phase) if phase else None

    @property
    def switching(self) -> bool:
        """Only phases the worker is still executing block normal controls.

        READY_UNARMED is the completed structural handover; FAILED_INITIALIZATION
        and BLOCKED_DRAIN wait for an explicit retry. None of them may leave the
        desktop permanently disabled.
        """
        return self.transition_phase in IN_PROGRESS_TRANSITION_PHASES

    @property
    def transition_retryable(self) -> bool:
        return self.transition_phase in RETRYABLE_TRANSITION_PHASES

    @property
    def transition_failure(self) -> Mapping[str, Any] | None:
        """The worker's redacted failure record for the last handover, if any."""
        transition = self.transition
        failure = transition.get("failure")
        if not isinstance(failure, Mapping):
            if transition.get("error_code") and self.transition_retryable:
                return cast(Mapping[str, Any], freeze({"code": str(transition["error_code"]), "stage": "UNKNOWN",
                                                       "category": "UNKNOWN", "summary": "", "retry_safe": False,
                                                       "corrective_action": "ACTION_INSPECT_WORKER_LOG",
                                                       "phase": self.transition_phase or ""}))
            return None
        chain = failure.get("chain", ())
        parts = []
        for link in chain:
            if not isinstance(link, Mapping):
                continue
            text = str(link.get("type", "")).rsplit(".", 1)[-1]
            detail = link.get("code") or link.get("module") or link.get("errno")
            parts.append(f"{text}[{detail}]" if detail else text)
        return cast(Mapping[str, Any], freeze({
            "code": str(failure.get("code", transition.get("error_code", "ENGINE_OPERATION_FAILED"))),
            "stage": str(failure.get("stage", "UNKNOWN")), "category": str(failure.get("category", "UNKNOWN")),
            "summary": " <- ".join(parts), "retry_safe": failure.get("retry_safe") is True,
            "corrective_action": str(failure.get("corrective_action", "ACTION_INSPECT_WORKER_LOG")),
            "phase": self.transition_phase or "",
        }))

    @property
    def worker_source_mismatch(self) -> bool:
        """True when the connected worker started from different sources than this desktop."""
        runtime = self.snapshot.get("runtime")
        if not self.connected or self.local_source_fingerprint is None or not isinstance(runtime, Mapping):
            return False
        remote = runtime.get("source_fingerprint")
        return isinstance(remote, str) and remote != self.local_source_fingerprint

    @property
    def normal_controls_enabled(self) -> bool:
        return self.ready and not self.switching

    @property
    def context_controls_enabled(self) -> bool:
        """A diagnostic preview does not initialize or arm an execution realm."""
        return self.safety_controls_enabled and not self.switching

    @property
    def safety_controls_enabled(self) -> bool:
        return self.connected and not self.snapshot.get("worker_initializing", False)

    @property
    def epoch(self) -> int:
        return int(self.snapshot.get("context_epoch", 0))

    @property
    def revision(self) -> int:
        return int(self.snapshot.get("config_revision", 0))

    @property
    def effective_caption(self) -> str:
        config = self.effective
        if not config:
            return "UNAVAILABLE"
        context = self.snapshot.get("effective_context") or {}
        epoch = context.get("epoch", self.epoch)
        subtype = config.get("product_subtype") or default_subtype_label(str(config.get("product_track", "SPOT")),
                                                                        str(config.get("region_policy", "GERMANY")))
        return f"{config.get('environment', 'UNKNOWN')} / {config.get('product_track', 'UNKNOWN')} / {subtype} / epoch {epoch}"

    @property
    def automation(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self.snapshot.get("automation") or {})


PRODUCT_SUBTYPES = ("SPOT", "PERPETUAL_NO_EXPIRY", "PERPETUAL_STYLE_FUTURE", "DATED_FUTURE")
PAPER_SCOPES = {"SPOT": "paper-book", "PERPETUAL_NO_EXPIRY": "paper-perpetual-book",
                "PERPETUAL_STYLE_FUTURE": "paper-perpetual-style-book", "DATED_FUTURE": "paper-dated-futures-book"}


def default_subtype_label(product_track: str, region_policy: str) -> str:
    if product_track == "SPOT":
        return "SPOT"
    return "PERPETUAL_STYLE_FUTURE" if region_policy == "GERMANY" else "PERPETUAL_NO_EXPIRY"


@dataclass(frozen=True, slots=True)
class SwitchSelection:
    """A selector proposes; the worker's capability resolver decides.

    No policy rule lives here: the worker answers a GERMANY/PERPETUAL request with
    the resolver's explicit category (supported simulation, policy rejection,
    missing enrollment, missing implementation) instead of a UI-side guess.
    """

    product_track: str
    environment: str
    region_policy: str
    product_subtype: str | None = None

    def __post_init__(self) -> None:
        if (self.product_track not in ("SPOT", "PERPETUAL") or self.environment not in ("LIVE", "SANDBOX")
                or self.region_policy not in ("GLOBAL", "GERMANY")):
            raise RPCError("INVALID_SELECTION")
        if self.product_subtype is not None:
            if self.product_subtype not in PRODUCT_SUBTYPES:
                raise RPCError("INVALID_SELECTION")
            if (self.product_subtype == "SPOT") != (self.product_track == "SPOT"):
                raise RPCError("INVALID_SELECTION")

    @property
    def subtype(self) -> str:
        return self.product_subtype or default_subtype_label(self.product_track, self.region_policy)

    def candidate(self, effective: Mapping[str, Any]) -> dict[str, Any]:
        candidate = cast(dict[str, Any], thaw(effective))
        candidate.update({"product_track": self.product_track, "environment": self.environment,
                          "region_policy": self.region_policy, "product_subtype": self.subtype})
        if self.environment == "SANDBOX":
            candidate.update({"run_profile": "PAPER", "endpoint_profile_id": "paper-local",
                              "broker_profile_id": "paper-local", "credential_binding_id": None,
                              "execution_scope": [PAPER_SCOPES[self.subtype]]})
        elif effective.get("environment") != "LIVE":
            # A selector cannot fabricate the opaque binding, eligibility or route.
            raise RPCError("LIVE_ENROLLMENT_REQUIRED")
        return candidate


def balance_presentation(balance: Mapping[str, Any]) -> Mapping[str, str]:
    """Never call an absent or unvalued subtotal VERIFIED.

    ``scope_label`` is one of VERIFIED_SCOPE (complete, verified evidence),
    UNVERIFIED_SUBTOTAL (known gaps, staleness or conflicts) or NOT_INITIALIZED
    (no book/account evidence at all). ``provenance`` names the valuation source.
    """
    environment = str(balance.get("environment", "UNKNOWN"))
    coverage = str(balance.get("coverage", "UNKNOWN_UNIVERSE"))
    amount = balance.get("equity", balance.get("observed_equity", balance.get("total")))
    spendable = balance.get("active_book_spendable", balance.get("active_book_available"))
    if coverage == "COMPLETE_VERIFIED" and amount is not None:
        scope_label = "VERIFIED_SCOPE"
    elif coverage == "UNKNOWN_UNIVERSE" and amount is None:
        scope_label = "NOT_INITIALIZED"
    else:
        scope_label = "UNVERIFIED_SUBTOTAL"
    return cast(Mapping[str, str], freeze({"realm": "SIMULATED" if environment == "SANDBOX" else environment,
                   "headline": str(amount) if amount is not None else "—",
                   "coverage": coverage,
                   "scope_label": scope_label,
                   "provenance": str(balance.get("valuation_provenance", "UNKNOWN")),
                   "currency": str(balance.get("reporting_currency", "")),
                   "spendable": str(spendable) if spendable is not None else "—"}))
