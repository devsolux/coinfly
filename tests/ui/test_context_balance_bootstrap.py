import threading
import unittest
from decimal import Decimal
from unittest.mock import patch

from coinfly.i18n import MESSAGES, Translator
from coinfly.ipc.protocol import RPCError, freeze
from coinfly.ui.app import DesktopDependencyError, load_toolkit
from coinfly.ui.render_scheduler import RenderQueue, RenderScheduler
from coinfly.ui.state import DesktopState, SwitchSelection, balance_presentation


class ContextPresentationTests(unittest.TestCase):
    def test_live_to_paper_keeps_effective_live_and_safety_access(self) -> None:
        state = DesktopState(freeze({"sequence": 4, "context_epoch": 9, "config_revision": 3,
                                      "effective_config": {"environment": "LIVE", "product_track": "SPOT"},
                                      "proposed_config": {"environment": "SANDBOX", "product_track": "PERPETUAL"},
                                      "transition": {"state": "DRAINING"},
                                      "bootstrap": {"stage": "VERIFYING", "ready": False, "received_bytes": 100, "expected_bytes": 100}}), True)
        self.assertIn("LIVE / SPOT", state.effective_caption)
        self.assertFalse(state.ready)
        self.assertFalse(state.normal_controls_enabled)
        self.assertTrue(state.safety_controls_enabled)
        self.assertEqual(state.proposed["environment"], "SANDBOX")

    def test_missing_data_unavailable_balances_and_forbidden_selection(self) -> None:
        state = DesktopState()
        self.assertFalse(state.normal_controls_enabled)
        self.assertEqual(balance_presentation(state.snapshot["balance"])["headline"], "—")
        synthetic = balance_presentation({"environment": "SANDBOX", "coverage": "PARTIAL", "equity": "150.00"})
        self.assertEqual(synthetic["realm"], "SIMULATED")
        self.assertEqual(synthetic["scope_label"], "UNVERIFIED_SUBTOTAL")
        self.assertEqual(synthetic["spendable"], "—")
        # GERMANY/PERPETUAL is a legitimate paper request: the worker's resolver
        # answers it (perpetual-style futures are the EEA contract model).
        german = SwitchSelection("PERPETUAL", "SANDBOX", "GERMANY").candidate({"environment": "SANDBOX"})
        self.assertEqual(german["product_subtype"], "PERPETUAL_STYLE_FUTURE")
        self.assertEqual(german["execution_scope"], ["paper-perpetual-style-book"])
        with self.assertRaises(RPCError):
            SwitchSelection("SPOT", "SANDBOX", "GERMANY", "PERPETUAL_NO_EXPIRY")
        with self.assertRaisesRegex(RPCError, "LIVE_ENROLLMENT_REQUIRED"):
            SwitchSelection("SPOT", "LIVE", "GLOBAL").candidate({"environment": "SANDBOX"})

    def test_initial_unconfigured_snapshot_and_distinct_paper_books(self) -> None:
        state = DesktopState(freeze({"sequence": 0, "context_epoch": 0, "effective_config": None,
                                      "proposed_config": {"environment": "SANDBOX", "authority_id": "local-paper",
                                                          "product_track": "SPOT", "execution_scope": ["paper-book"]},
                                      "bootstrap": {"stage": "READY", "ready": True}}), True)
        self.assertEqual(state.effective, {})
        self.assertTrue(state.normal_controls_enabled)
        candidate = SwitchSelection("PERPETUAL", "SANDBOX", "GLOBAL").candidate(state.proposed)
        self.assertEqual(candidate["execution_scope"], ["paper-perpetual-book"])
        self.assertEqual(state.proposed["execution_scope"], ("paper-book",))

    def test_certified_major_version_never_silently_downgrades(self) -> None:
        with patch("coinfly.ui.app.importlib.metadata.version", return_value="5.2.2"):
            with self.assertRaises(DesktopDependencyError):
                load_toolkit()

    def test_locale_money_is_explicit_and_labels_complete(self) -> None:
        self.assertEqual(set(MESSAGES["en"]), set(MESSAGES["de"]))
        de = Translator("de")
        self.assertEqual(de.parse_decimal("10,25"), Decimal("10.25"))
        self.assertEqual(de.decimal("1020.25"), "1.020,25")
        self.assertEqual(Translator("en").decimal(None), "—")
        with self.assertRaises(ValueError):
            de.parse_decimal("1.020,25")


class RenderHandoffTests(unittest.TestCase):
    def test_snapshot_coalescing_does_not_overwrite_alarms_or_mutate_state(self) -> None:
        queue = RenderQueue(2)
        original = {"sequence": 1, "balance": {"equity": "10"}}
        queue.put("snapshot", original)
        original["balance"]["equity"] = "900"
        first = queue.take()[0]
        self.assertEqual(first.payload["balance"]["equity"], "10")
        for seq in range(100):
            queue.put("snapshot", {"sequence": seq})
        queue.put("alarm", {"code": "UNKNOWN"})
        queue.put("response", {"operation_id": "T1"})
        self.assertFalse(queue.put("alarm", {"code": "SECOND_UNKNOWN"}))
        self.assertLessEqual(len(queue), 4)
        events = queue.take()
        self.assertEqual([event.kind for event in events], ["resnapshot_required", "alarm", "response", "snapshot"])
        self.assertEqual(events[-1].payload["sequence"], 99)

    def test_tk_render_callback_rejects_reader_thread(self) -> None:
        class Root:
            def after(self, *_):
                pass
        scheduler = RenderScheduler(Root(), RenderQueue(), lambda _: None)
        scheduler.start()
        errors = []
        def wrong_thread():
            try:
                scheduler._tick()
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join()
        self.assertEqual(errors, ["Tk rendering must run on the main thread"])
