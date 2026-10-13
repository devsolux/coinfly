"""Explicit absence of external certification is a closed release gate.

These are compiled/package-reviewed trust anchors, never GUI, environment, or
command-line settings. An unsigned source checkout cannot certify itself.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

# Populated only by a reviewed native release, after signed binary qualification.
# Deliberately empty in 1.0.0 source delivery: there is no publisher identity here.
NATIVE_BRIDGE_SHA256: Mapping[str, str] = MappingProxyType({})
CERTIFIED_REGION_CHANNELS: frozenset[str] = frozenset()
CERTIFIED_LIVE_GATEWAYS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ReleaseGate:
    code: str
    satisfied: bool
    evidence_required: str


def live_release_gates() -> tuple[ReleaseGate, ...]:
    return (
        ReleaseGate(
            "NATIVE_SIGNING_UNCERTIFIED",
            bool(NATIVE_BRIDGE_SHA256),
            "Signed macOS ARM64/Windows AMD64 worker and vault bridge qualification",
        ),
        ReleaseGate(
            "REGION_UNVERIFIED",
            bool(CERTIFIED_REGION_CHANNELS),
            "Authenticated broker-issued, account-bound legal/service eligibility evidence",
        ),
        ReleaseGate(
            "LIVE_GATEWAY_UNCERTIFIED",
            bool(CERTIFIED_LIVE_GATEWAYS),
            "Active account gateway, units, order, protection, and reconciliation certification",
        ),
    )
