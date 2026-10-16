"""Live arming: explicit mandate, ordered prerequisites, no bypass.

These tests never authenticate or place an order. They pin that a LIVE
configuration is reported with every missing external artifact named, that the
mandate is bounded by the configured limits, and that nothing local can flip a
closed gate.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest import TestCase

from coinfly.bootstrap.manager import BootstrapManager
from coinfly.config.models import EngineConfig, default_config
from coinfly.domain.context import DomainError, Environment, ProductTrack, RunProfile
from coinfly.domain.products import ProductSubtype
from coinfly.ipc.protocol import OWNER_METHODS, READ_METHODS, RPCRequest
from coinfly.operations.live_arming import (
    MANDATE_STATEMENT,
    LiveMandate,
    LiveMandateStore,
    live_arming_prerequisites,
)
from coinfly.venues.capability import LocalQualifications
from coinfly.worker.paths import WorkerPaths
from coinfly.worker.supervisor import WorkerSupervisor
from tests.support import local_peer

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def live_config() -> EngineConfig:
    base = default_config().model_dump()
    return EngineConfig.model_validate({**base, "environment": Environment.LIVE, "run_profile": RunProfile.STANDARD,
                                        "credential_binding_id": "a" * 32, "endpoint_profile_id": "coinbase-advanced-spot-execution",
                                        "execution_scope": ("portfolio-1",), "broker_profile_id": "coinbase-advanced-spot-execution"})


def mandate(**overrides: Any) -> LiveMandate:
    values: dict[str, Any] = dict(authority_id="local-paper", approver="Baris", statement=MANDATE_STATEMENT, product_track=ProductTrack.SPOT,
                                  product_subtype=ProductSubtype.SPOT, portfolio_id="portfolio-1", allowed_instruments=("BTC-USD",),
                                  max_order_notional=Decimal("10"), max_gross_exposure=Decimal("1"), max_daily_loss=Decimal("20"),
                                  approved_at=NOW, expires_at=NOW + timedelta(days=1))
    values.update(overrides)
    return LiveMandate.model_validate(values)


class PrerequisiteTests(TestCase):
    def test_every_external_artifact_is_named_in_order(self) -> None:
        items = live_arming_prerequisites(live_config(), now=NOW, evidence=None, local=LocalQualifications(), credential_bound=False,
                                          dataset_ready=(False, "DATASET_NOT_READY"), kernel_ready=(False, "NATIVE_KERNEL_UNAVAILABLE"),
                                          mandate=None)
        codes = [item.code for item in items]
        self.assertEqual(codes[:3], ["NATIVE_SIGNING_UNCERTIFIED", "REGION_UNVERIFIED", "LIVE_GATEWAY_UNCERTIFIED"])
        self.assertIn("EXECUTION_CREDENTIAL_BOUND", codes)
        self.assertIn("ACCOUNT_EVIDENCE_MISSING", codes)
        self.assertIn("KEY_PERMISSIONS_UNVERIFIED", codes)
        self.assertIn("LIVE_ADAPTER_QUALIFICATION_RUN", codes)
        self.assertEqual(codes[-1], "LIVE_MANDATE_REQUIRED")
        satisfied = [item.code for item in items if item.satisfied]
        self.assertEqual(satisfied, ["LIVE_ENDPOINT_PROFILE"])  # the only local artifact this checkout provides
        capability = next(item for item in items if item.code.startswith("CAPABILITY_"))
        self.assertFalse(capability.satisfied)
        self.assertIn("ACCOUNT_EVIDENCE_MISSING", capability.detail)
        kinds = {item.code: item.kind for item in items}
        self.assertEqual(kinds["NATIVE_SIGNING_UNCERTIFIED"], "external")
        self.assertEqual(kinds["LIVE_MANDATE_REQUIRED"], "operator")

    def test_mandate_is_bounded_by_the_configured_limits(self) -> None:
        config = live_config()
        self.assertEqual(mandate().check(config, now=NOW), ())
        self.assertIn("LIVE_MANDATE_EXCEEDS_CONFIGURED_LIMITS", mandate(max_order_notional=Decimal("11")).check(config, now=NOW))
        self.assertIn("LIVE_MANDATE_EXPIRED", mandate().check(config, now=NOW + timedelta(days=2)))
        self.assertIn("LIVE_MANDATE_PORTFOLIO_MISMATCH", mandate(portfolio_id="other").check(config, now=NOW))
        self.assertIn("LIVE_MANDATE_STATEMENT_MISMATCH", mandate(statement="yes please").check(config, now=NOW))
        self.assertIn("LIVE_MANDATE_ENVIRONMENT_MISMATCH", mandate().check(default_config(), now=NOW))
        # A covering mandate still leaves every external artifact unsatisfied.
        items = live_arming_prerequisites(config, now=NOW, evidence=None, local=LocalQualifications(), credential_bound=True,
                                          dataset_ready=(True, "READY"), kernel_ready=(True, "NATIVE_KERNEL_VERIFIED"), mandate=mandate())
        self.assertEqual([item.code for item in items if not item.satisfied][:3],
                         ["NATIVE_SIGNING_UNCERTIFIED", "REGION_UNVERIFIED", "LIVE_GATEWAY_UNCERTIFIED"])
        self.assertTrue(next(item for item in items if item.code == "LIVE_MANDATE_COVERS_CONFIGURATION").satisfied)


class WorkerLiveRpcTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="coinfly-live-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.worker = WorkerSupervisor(paths=WorkerPaths(root / "worker"), socket_path=root / "w.sock", bootstrap=BootstrapManager(home=root))
        self.addCleanup(self.worker.close)

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = RPCRequest.parse({"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}, "epoch": 0,
                                    "deadline_ms": int(time.time() * 1000) + 10000})
        return cast(dict[str, Any], self.worker.handle(request, local_peer()))

    def test_prerequisites_report_and_mandate_lifecycle(self) -> None:
        self.assertIn("live.prerequisites", READ_METHODS)
        self.assertIn("live.record_mandate", OWNER_METHODS)
        report = self.rpc("live.prerequisites", {"config": live_config().model_dump(mode="json")})
        self.assertFalse(report["ready"])
        self.assertEqual(report["next_required"], "NATIVE_SIGNING_UNCERTIFIED")
        self.assertIn("LIVE_MANDATE_REQUIRED", report["operator"])
        self.assertEqual(report["environment"], "LIVE")
        with self.assertRaises(DomainError) as unconfirmed:
            self.rpc("live.record_mandate", {"approver": "Baris"})
        self.assertEqual(unconfirmed.exception.code, "LIVE_MANDATE_CONFIRMATION_REQUIRED")
        params = {"confirmation": True, "approver": "Baris", "statement": MANDATE_STATEMENT, "product_track": "SPOT", "product_subtype": "SPOT",
                  "portfolio_id": "portfolio-1", "allowed_instruments": ["BTC-USD"], "max_order_notional": "10", "max_gross_exposure": "1",
                  "max_daily_loss": "20", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()}
        recorded = self.rpc("live.record_mandate", params)
        self.assertEqual(len(recorded["digest"]), 64)
        report = self.rpc("live.prerequisites", {"config": live_config().model_dump(mode="json")})
        self.assertNotIn("LIVE_MANDATE_REQUIRED", report["operator"])
        self.assertFalse(report["ready"])  # external artifacts are still missing: no bypass
        self.assertEqual(report["external"][:3], ["NATIVE_SIGNING_UNCERTIFIED", "REGION_UNVERIFIED", "LIVE_GATEWAY_UNCERTIFIED"])
        store = LiveMandateStore(self.worker.writer)
        self.assertEqual(len(store.active("local-paper", now=datetime.now(timezone.utc))), 1)
        self.assertTrue(self.rpc("live.revoke_mandate", {"mandate_id": recorded["mandate_id"]})["revoked"])
        self.assertEqual(store.active("local-paper", now=datetime.now(timezone.utc)), ())
        with self.assertRaises(DomainError) as bad:
            self.rpc("live.record_mandate", {**params, "statement": "I agree"})
        self.assertEqual(bad.exception.code, "LIVE_MANDATE_STATEMENT_MISMATCH")
        # A LIVE context can still not be previewed from this checkout: the
        # capability resolver refuses it with the enrollment/qualification codes.
        with self.assertRaises(DomainError) as refused:
            self.rpc("context.preview", {"config": live_config().model_dump(mode="json"), "expected_revision": 0, "expected_epoch": 0})
        self.assertIn(refused.exception.code, ("LIVE_CAPABILITY_UNAVAILABLE", "ACCOUNT_EVIDENCE_MISSING", "REGION_UNVERIFIED",
                                               "LIVE_REQUIRES_STANDARD_PROFILE", "ADAPTER_UNAVAILABLE"))
