"""Overview and Settings render provenance, research, recovery, catalog and build identity from the worker snapshot.

The text helpers are pure functions of the snapshot and the translator, so they
are exercised here without Tk; the native smoke test covers the widgets.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from coinfly.i18n import Translator
from coinfly.observability.diagnostics import build_identity
from coinfly.ui.views.settings import SettingsView
from coinfly.ui.widgets.automation_panel import AutomationPanel


def stub(language: str = "en") -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(tr=Translator(language)))


class AutomationStatusTextTests(TestCase):
    def test_research_line_reports_reward_windows_and_training(self) -> None:
        status = {"research": {"session_id": "abcdef1234", "windows_closed": 7, "windows_fed": 6, "pending_windows": 1,
                               "horizon_exceeded": 0, "last_training": {"frames": 3, "reward": "0.01"}}}
        text = AutomationPanel._research_text(stub(), status)
        self.assertIn("Research session: abcdef12", text)
        self.assertIn("Reward windows: 7 · fed 6 · pending 1 · credit horizon exceeded 0", text)
        self.assertIn("Last training: frames 3 reward 0.01", text)
        self.assertEqual(AutomationPanel._research_text(stub(), {"research": None}), "")
        self.assertIn("Forschungssitzung", AutomationPanel._research_text(stub("de"), status))

    def test_recovery_line_shows_policy_last_decision_and_restoration(self) -> None:
        manual = {"recovery": {"policy": {"auto_resume": False, "max_resumptions": 0}, "last": None, "restoration": None}}
        self.assertIn("Recovery policy: manual re-enable only", AutomationPanel._recovery_text(stub(), manual))
        auto = {"recovery": {"policy": {"auto_resume": True, "mandate_expires_at": "2026-09-22T12:00:00+00:00", "max_resumptions": 2},
                             "last": {"resumed": False, "reason": "RECOVERY_MANDATE_EXPIRED"},
                             "restoration": {"infrastructure": {"ready": True, "code": "STORAGE_READY"},
                                             "financial": {"ready": False, "code": "OPEN_INTENTS"},
                                             "neural": {"ready": True, "code": "PIPELINE_FIXTURE"}}}}
        text = AutomationPanel._recovery_text(stub(), auto)
        self.assertIn("auto-resume · mandate expires 2026-09-22T12:00:00+00:00 · resumptions ≤ 2", text)
        self.assertIn("Last recovery decision: RECOVERY_MANDATE_EXPIRED (no)", text)
        self.assertIn("Restoration: ✓ infrastructure: STORAGE_READY · ✗ financial: OPEN_INTENTS · ✓ neural: PIPELINE_FIXTURE", text)

    def test_catalog_exclusions_are_named_not_hidden(self) -> None:
        feed = {"catalog": {"entries": 12, "excluded": {"VENUE_PRODUCT_SUBTYPE_UNVERIFIED": 1, "VENUE_PRODUCT_INVALID": 2},
                            "subtype_unverified": ["BIT-20DEC30-CDE"]}}
        text = AutomationPanel._catalog_text(stub(), feed)
        self.assertIn("Catalog: 12 (excluded: VENUE_PRODUCT_SUBTYPE_UNVERIFIED 1, VENUE_PRODUCT_INVALID 2)", text)
        self.assertIn("subtype unverified (excluded): BIT-20DEC30-CDE", text)
        self.assertEqual(AutomationPanel._catalog_text(stub(), {}), "")
        # An empty catalog for this contract type explains itself.
        futures = {"catalog": {"entries": 0, "excluded": {}, "other_subtypes": {"DATED_FUTURE": 88, "PERPETUAL_STYLE_FUTURE": 30},
                               "last_error": "HTTP_READ_FAILED"}}
        text = AutomationPanel._catalog_text(stub(), futures)
        self.assertIn("other contract types (not this context): DATED_FUTURE 88, PERPETUAL_STYLE_FUTURE 30", text)
        self.assertIn("catalog refresh failed: HTTP_READ_FAILED", text)


class AutomationBlockingTextTests(TestCase):
    """The Overview says why automation is not trading, and what the operator can do about it."""

    def test_blocking_components_carry_an_actionable_hint(self) -> None:
        status = {"components": [{"component": "feed", "ready": True, "code": "FEED_FRESH"},
                                 {"component": "kernel", "ready": False, "code": "NATIVE_KERNEL_UNAVAILABLE"},
                                 {"component": "strategy", "ready": False, "code": "NATIVE_KERNEL_UNAVAILABLE"}]}
        text = AutomationPanel._components_text(stub(), status)
        self.assertIn("✗ Native kernel: NATIVE_KERNEL_UNAVAILABLE", text)
        self.assertEqual(text.count("python dev.py kernel"), 1)  # one hint per condition
        self.assertIn("nativen Kernel", AutomationPanel._components_text(stub("de"), status))
        ready = {"components": [{"component": "feed", "ready": True, "code": "FEED_FRESH"}]}
        self.assertNotIn("→", AutomationPanel._components_text(stub(), ready))

    def test_universe_lists_threshold_rejections_before_missing_books(self) -> None:
        reasons = [{"instrument": f"A{index}-USD", "eligible": False, "codes": ["NO_TWO_SIDED_OBSERVATION"],
                    "detail": "NO_TWO_SIDED_OBSERVATION"} for index in range(8)]
        reasons.append({"instrument": "BIP-20DEC30-CDE", "eligible": False,
                        "codes": ["MIN_ORDER_ABOVE_SLOT_CAP", "NO_TWO_SIDED_OBSERVATION"],
                        "detail": "MIN_ORDER_ABOVE_SLOT_CAP, NO_TWO_SIDED_OBSERVATION (min_order_notional=865.42; max_slot_notional<25.00)"})
        text = AutomationPanel._universe_text(stub(), {"universe": {"selected": [], "reasons": reasons}})
        self.assertTrue(text.startswith("Universe: — · Rejected 9: NO_TWO_SIDED_OBSERVATION 9, MIN_ORDER_ABOVE_SLOT_CAP 1"), text)
        self.assertIn("\nBIP-20DEC30-CDE (MIN_ORDER_ABOVE_SLOT_CAP", text)
        self.assertIn("One contract costs more than a slot may ever hold", text)
        plain = AutomationPanel._universe_text(stub(), {"universe": {"selected": ["BTC-USD"], "reasons": [
            {"instrument": "ETH-USD", "eligible": False, "codes": ["NOT_PERMITTED"]}]}})
        self.assertIn("ETH-USD (NOT_PERMITTED)", plain)
        self.assertEqual(AutomationPanel._universe_text(stub(), {"universe": None}), "")

    def test_failure_names_the_cause_origin_and_next_step(self) -> None:
        status = {"failure": {"code": "OPERATION_FAILED", "stage": "SETUP", "summary": "ValidationError",
                              "origin": "coinfly/market/coinbase_public.py:233 catalog_entry_from_product",
                              "corrective_action": "ACTION_INSPECT_WORKER_LOG"}}
        text = AutomationPanel._failure_text(stub(), status)
        self.assertIn("Automation failure: OPERATION_FAILED · Failed stage: SETUP · Cause: ValidationError", text)
        self.assertIn("(coinfly/market/coinbase_public.py:233 catalog_entry_from_product)", text)
        self.assertIn("What to do: Inspect the worker log", text)
        self.assertEqual(AutomationPanel._failure_text(stub(), {"failure": None}), "")


class SettingsIdentityTextTests(TestCase):
    def test_identity_block_compares_worker_and_desktop_builds_and_shows_provenance(self) -> None:
        desktop = dict(build_identity())
        snapshot = {"build": desktop, "retirement": {"mode": "GRACEFUL", "state": "WAITING_FLAT", "since": None},
                    "automation": {"pipeline_test_mode": True,
                                   "provenance": {"mode": "BASELINE_INFERENCE", "graph": {"neurons": 166700, "edges": 25582938},
                                                  "kernel": {"binary_sha256": "0123456789abcdef"}, "checkpoint": {"kind": "UNTRAINED_BASELINE"},
                                                  "feature_transform": "sensory-v1"},
                                   "components": [{"component": "kernel", "ready": False, "code": "NATIVE_KERNEL_UNAVAILABLE"}]}}
        text, warning = SettingsView._identity_text(stub(), snapshot)
        self.assertFalse(warning)
        self.assertIn("worker and desktop builds agree", text)
        self.assertIn(f"Worker build: {desktop['application_id']} {desktop['version']} · protocol {desktop['protocol_version']}", text)
        self.assertIn("Retirement: mode GRACEFUL · state WAITING_FLAT", text)
        self.assertIn("Provenance: Mode BASELINE_INFERENCE · Graph identity 166700/25582938 · Kernel 0123456789ab · Checkpoint untrained baseline · sensory-v1", text)
        self.assertIn("PIPELINE TEST MODE", text)
        self.assertIn("Neural readiness: ✗ Native kernel: NATIVE_KERNEL_UNAVAILABLE", text)
        # A different worker build is a warning, never silently accepted.
        mismatched = {**snapshot, "build": {**desktop, "source_fingerprint": "f" * 64}}
        text, warning = SettingsView._identity_text(stub(), mismatched)
        self.assertTrue(warning)
        self.assertIn("WORKER BUILD MISMATCH", text)
        self.assertEqual(SettingsView._identity_text(stub(), {}), ("No worker snapshot yet.", False))
