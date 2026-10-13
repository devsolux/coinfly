"""Opt-in tests against real Tk widgets; no worker, credentials, or orders."""

import json
import runpy
from pathlib import Path

import pytest

from coinfly.ipc.protocol import freeze
from coinfly.ui.app import CoinflyApp, load_toolkit
from coinfly.ui.render_scheduler import RenderEvent
from coinfly.ui.state import DesktopState


@pytest.fixture
def desktop(request, monkeypatch):
    if not request.config.getoption("--native-ui"):
        pytest.skip("Native desktop smoke tests require ./dev.sh test-ui")
    # Keep the real UI and render scheduler, but never contact an operator worker.
    monkeypatch.setattr("coinfly.ui.app.DesktopClient.start", lambda self: None)
    monkeypatch.setattr("coinfly.ui.app.DesktopClient.close", lambda self: None)
    app = CoinflyApp(load_toolkit())
    try:
        app.root.update_idletasks()
        yield app
    finally:
        app.close()


def assert_visible_view(app, name):
    app.root.update_idletasks()
    surface = app.views[name].frame
    # Scrollable views wrap their content in a canvas and an outer frame.
    while surface.master is not app.host:
        surface = surface.master
    assert app.host.winfo_children()[-1] is surface, f"{name} is covered by another view"


def test_native_navigation_and_language_rebuild(desktop):
    assert_visible_view(desktop, "overview")
    for language in ("en", "de", "en"):
        desktop.set_language(language)
        for name in ("settings", "overview", "activity", "risk", "overview"):
            desktop.show_view(name)
            assert_visible_view(desktop, name)
            assert desktop.nav[name].cget("text") == desktop.tr(name)


def test_native_missing_dataset_keeps_safety_access(desktop):
    desktop.state = DesktopState(freeze({
        "sequence": 1, "context_epoch": 0, "config_revision": 0,
        "bootstrap": {"stage": "BLOCKED", "ready": False, "reason": "PUBLISHER_TRUST_ROOT_MISSING"},
    }), True)
    desktop._refresh()
    setup = desktop._bootstrap
    assert setup is not None
    assert "PUBLISHER_TRUST_ROOT_MISSING" in setup.evidence.cget("text")
    assert desktop.overview.preview.cget("state") == "disabled"
    setup.open_safety()
    assert_visible_view(desktop, "risk")
    assert desktop.risk.arm.cget("state") == "disabled"
    assert setup.window.state() == "withdrawn"
    assert desktop.risk.stop.cget("state") == "normal"
    assert desktop.risk.pause.cget("state") == "normal"
    setup.open_settings()
    assert_visible_view(desktop, "settings")
    assert desktop.settings.enroll.cget("state") == "disabled"


def test_native_credentials_validation_visibility_and_local_submission(desktop, monkeypatch):
    from tests.security.test_desktop_credentials import KEY, secret
    desktop.show_view("settings")
    view = desktop.settings
    assert view.api_key.cget("show") == "•"
    assert view.api_secret.cget("show") == "•"
    view.key_value.set("invalid")
    view.secret_value.set(secret())
    assert view.enroll.cget("state") == "disabled"
    view.key_value.set(KEY)
    assert view.enroll.cget("state") == "normal"
    view._visibility()
    assert view.api_secret.cget("show") == ""
    submissions = []
    monkeypatch.setattr(desktop.credential_worker, "submit", lambda key, material: submissions.append((key, material)) or True)
    monkeypatch.setattr(desktop.client, "submit", lambda *_args, **_kwargs: pytest.fail("Secret crossed IPC"))
    view._save()
    assert len(submissions) == 1
    assert view.key_value.get() == view.secret_value.get() == ""
    assert view.api_secret.cget("show") == "•"
    assert view.enroll.cget("state") == "disabled"


def test_native_saved_pair_does_not_flag_cleared_replacement_fields(desktop):
    from coinfly.ui.theme import ACCENT

    desktop.show_view("settings")
    view = desktop.settings
    desktop._render(RenderEvent("credentials", freeze({"code": "SECURELY_STORED"})))
    assert view.validation.cget("text") == desktop.tr("SECURELY_STORED")
    assert view.validation.cget("text_color") == ACCENT
    assert view.vault_status.cget("text_color") == ACCENT
    assert view.enroll.cget("state") == "disabled"
    # Editing a replacement still receives validation, without reloading secrets.
    view.key_value.set("invalid replacement")
    assert view.validation.cget("text") != desktop.tr("SECURELY_STORED")
    assert view.enroll.cget("state") == "disabled"
    view.key_value.set("")
    desktop._render(RenderEvent("credentials", freeze({"code": "OS_KEYRING_LOCKED_OR_DENIED"})))
    assert view.vault_status.cget("text") == desktop.tr("OS_KEYRING_LOCKED_OR_DENIED")


def test_native_connection_test_is_explicit_and_separate_from_storage_status(desktop, monkeypatch):
    from coinfly.ui.theme import ACCENT, DANGER, MUTED

    desktop.show_view("settings")
    view = desktop.settings
    # No verifier was injected into this smoke fixture: the button is disabled
    # and opening Settings has not triggered any keychain or network access.
    assert view.test_connection.cget("state") == "disabled"
    assert view.connection_status.cget("text") == desktop.tr("COINBASE_AUTH_UNCHECKED")
    assert view.connection_status.cget("text_color") == MUTED
    desktop.connection_test_available = True
    desktop._refresh()
    assert view.test_connection.cget("state") == "normal"
    requests = []
    monkeypatch.setattr(desktop.credential_worker, "submit_connection_test", lambda: requests.append(1) or True)
    monkeypatch.setattr(desktop.client, "submit", lambda *_args, **_kwargs: pytest.fail("Connection test crossed IPC"))
    desktop.test_connection()
    assert requests == [1]
    assert view.connection_status.cget("text") == desktop.tr("COINBASE_AUTH_CHECKING")
    desktop._render(RenderEvent("connection_test", freeze({"code": "COINBASE_AUTH_VERIFIED", "http_status": 200,
                                                           "verified": True, "authorizes_live_execution": False})))
    assert view.connection_status.cget("text") == desktop.tr("COINBASE_AUTH_VERIFIED") + " (HTTP 200)"
    assert view.connection_status.cget("text_color") == ACCENT
    # Remote verification changes neither the storage badge nor any live gate.
    assert view.vault_status.cget("text") == desktop.tr("CREDENTIALS_UNCHECKED")
    assert not desktop.state.normal_controls_enabled
    desktop._render(RenderEvent("connection_test", freeze({"code": "COINBASE_AUTH_REJECTED", "http_status": 401})))
    assert view.connection_status.cget("text") == desktop.tr("COINBASE_AUTH_REJECTED") + " (HTTP 401)"
    assert view.connection_status.cget("text_color") == DANGER
    desktop._render(RenderEvent("credentials", freeze({"code": "SECURELY_STORED"})))
    assert view.connection_status.cget("text").startswith(desktop.tr("COINBASE_AUTH_REJECTED"))


def test_native_blocked_setup_allows_history_and_context_diagnostics(desktop, monkeypatch):
    desktop.state = DesktopState(freeze({
        "sequence": 1, "effective_config": None,
        "proposed_config": {"environment": "SANDBOX", "product_track": "SPOT", "region_policy": "GERMANY"},
        "bootstrap": {"stage": "BLOCKED", "ready": False, "reason": "PUBLISHER_TRUST_ROOT_MISSING"},
        "release_gates": [{"code": "REGION_UNVERIFIED", "satisfied": False},
                          {"code": "LIVE_GATEWAY_UNCERTIFIED", "satisfied": False}],
    }), True)
    desktop._refresh()
    desktop.open_setup()
    desktop._bootstrap.open_history()
    assert_visible_view(desktop, "activity")
    assert desktop.activity.refresh.cget("state") == "normal"
    assert desktop._bootstrap.window.state() == "withdrawn"
    desktop.show_view("settings")
    assert desktop.settings.environment.cget("state") == "normal"
    assert desktop.tr("no_effective_context") in desktop.settings.environment_status.cget("text")
    monkeypatch.setattr(desktop.client, "submit", lambda *_args, **_kwargs: pytest.fail("Unenrolled live switch sent"))
    desktop.settings._environment(desktop.tr("live_environment"))
    assert desktop.status.cget("text") == desktop.tr("LIVE_ENROLLMENT_REQUIRED")
    assert not desktop.state.normal_controls_enabled


def test_native_context_preview_is_available_before_dataset_activation(desktop, monkeypatch):
    from coinfly.worker.supervisor import default_config

    config = default_config().model_dump(mode="json")
    desktop.state = DesktopState(freeze({"sequence": 1, "effective_config": None,
        "proposed_config": config, "bootstrap": {"stage": "BLOCKED", "ready": False}}), True)
    submitted = []
    monkeypatch.setattr(desktop.client, "submit", lambda *args, **kwargs: submitted.append(args) or "request")
    desktop.settings._environment(desktop.tr("paper_environment"))
    assert submitted[0][0] == "context.preview"
    desktop._render(RenderEvent("response", freeze({"request_id": "request", "method": "context.preview",
        "result": {"target": config, "bootstrap_ready": False}})))
    assert desktop._switch_dialog.confirm.cget("state") == "disabled"


def test_native_database_failure_stops_spinner_and_keeps_settings_access(desktop):
    desktop.state = DesktopState(freeze({"sequence": 1, "worker_initializing": True,
        "bootstrap": {"component": "database", "stage": "RETRYING", "ready": False,
                      "reason": "DATABASE_LOCKED_RETRYING"}}), True)
    desktop._refresh()
    dialog = desktop._bootstrap
    assert dialog._indeterminate
    assert "Database locked by another process" in dialog.evidence.cget("text")
    desktop.state = DesktopState(freeze({"sequence": 2, "worker_initializing": True,
        "bootstrap": {"component": "database", "stage": "BLOCKED", "ready": False,
                      "reason": "DATABASE_INITIALIZATION_TIMEOUT"}}), True)
    desktop._refresh()
    assert not dialog._indeterminate
    assert dialog.buttons[1].cget("state") == "normal"
    assert not desktop.state.safety_controls_enabled
    dialog.open_settings()
    assert_visible_view(desktop, "settings")


def test_native_local_dataset_labels_pause_and_ready(desktop):
    def update(stage):
        desktop.state = DesktopState(freeze({"sequence": 1,
            "bootstrap": {"stage": stage, "ready": stage == "READY", "verification": "LOCAL_SOURCE_LOCKS",
                          "asset": "edges.feather", "bytes_completed": 10, "bytes_total": 100}}), True)
        desktop._refresh()

    update("DOWNLOADING")
    dialog = desktop._bootstrap
    assert "original source hashes" in dialog.trust.cget("text")
    assert "edges.feather" in dialog.details.cget("text")
    assert dialog.buttons[0].cget("state") == "normal"
    assert dialog.buttons[1].cget("state") == "disabled"
    update("PAUSED")
    assert dialog.buttons[1].cget("text") == "Resume"
    assert dialog.buttons[1].cget("state") == "normal"
    assert dialog.buttons[0].cget("state") == "disabled"
    update("READY")
    assert desktop._bootstrap is None
    assert "LOCAL DEVELOPMENT" in desktop.root.title()
    assert "PAPER ONLY" in desktop.footer.cget("text")
    assert desktop.state.normal_controls_enabled


def test_native_automation_panel_reflects_worker_status_and_gates_commands(desktop, monkeypatch):
    sent = []
    monkeypatch.setattr(desktop, "send", lambda method, params, **kwargs: sent.append((method, dict(params))) or "req")
    effective = {"config": {"environment": "SANDBOX", "product_track": "SPOT", "region_policy": "GERMANY", "product_subtype": "SPOT",
                            "reporting_currency": "USD"}}
    base = {"sequence": 1, "context_epoch": 1, "config_revision": 1, "worker_initializing": False, "armed": True,
            "bootstrap": {"stage": "READY", "ready": True}, "effective_config": effective,
            "effective_context": {"environment": "SANDBOX", "product_track": "SPOT", "epoch": 1}, "engine_state": "ACTIVE",
            "transition": None, "slots": [], "balance": {"reporting_currency": "USD", "total": "100", "coverage": "COMPLETE_VERIFIED"}}
    desktop.state = DesktopState(freeze({**base, "automation": {"state": "NOT_CONFIGURED", "configured": False, "enabled": False}}), True)
    desktop._refresh()
    panel = desktop.overview.automation
    assert panel.state_label.cget("text") == desktop.tr("AUTOMATION_NOT_CONFIGURED")
    assert panel.buttons["configure"].cget("state") == "normal"
    assert panel.buttons["enable"].cget("state") == "disabled"
    assert panel.buttons["stop"].cget("state") == "disabled"
    assert "Not configured" in panel.strategy.cget("text")
    desktop.automation_action("configure")
    assert sent[-1][0] == "automation.configure"
    assert sent[-1][1]["preset"] == "research-paper-spot"
    assert sent[-1][1]["feed"]["kind"] == "COINBASE_PUBLIC_WEBSOCKET"
    running = {
        "state": "RUNNING", "configured": True, "enabled": True, "entries_paused": False, "restart_reason": None,
        "strategy": {"identity": "pipeline-fixture@1", "label": "PIPELINE FIXTURE", "controller_profile": "fixture-sequence-v1",
                     "mapping_version": "fixed-decoder-spot-step-v1", "research_only": True, "research_mode": False,
                     "learning_enabled": False, "approved_capital": "100"},
        "components": [{"component": "feed", "ready": True, "code": "FEED_FRESH"}, {"component": "kernel", "ready": False, "code": "NATIVE_KERNEL_UNAVAILABLE"}],
        "feed": {"label": "SYNTHETIC FIXTURE — NOT MARKET DATA", "state": "CONNECTED", "last_message_at": "2026-09-21T12:00:00Z",
                 "sequence_gaps": 0, "reconnects": 0, "resynchronizations": 0, "real_market_data": False},
        "universe": {"selected": ["BTC-USD"], "reasons": [{"instrument": "ETH-USD", "eligible": False, "codes": ["NOT_PERMITTED"]}]},
        "last_observation_at": "2026-09-21T12:00:00Z",
        "last_decision": {"instrument": "BTC-USD", "side": "BUY", "outcome": "DISPATCHED", "outcome_code": "ADMITTED",
                          "admission_reason": "not_applicable", "decided_at": "2026-09-21T12:00:01Z", "pipeline_fixture": True},
        "execution_health": {"open_intents": 1, "unknown": 0, "fills": 0, "fenced": False}, "ticks": 9, "failure": None,
        "slots": [{"slot_id": "s1", "instrument": "BTC-USD", "strategy": "pipeline-fixture@1", "selection_reason": "rank 1", "lifecycle": "ACTIVE",
                   "ownership_state": "ACTIVE", "target_weight": "0.05", "actual_quantity": "0", "allocated_budget": "34", "warm": True,
                   "observations": 9, "last_decision": {"side": "BUY", "outcome": "DISPATCHED"}, "blocking_reason": ""}],
        "pipeline_test_mode": True,
        "provenance": {"mode": "BASELINE_INFERENCE", "graph": {"neurons": 166700, "edges": 25582938}, "kernel": {"binary_sha256": "0123456789abcdef"},
                       "checkpoint": {"kind": "UNTRAINED_BASELINE"}, "feature_transform": "sensory-v1"},
        "research": None,
        "recovery": {"policy": {"auto_resume": False, "max_resumptions": 0}, "last": None, "restoration": None},
    }
    running["feed"]["catalog"] = {"entries": 3, "excluded": {"VENUE_PRODUCT_SUBTYPE_UNVERIFIED": 1}, "subtype_unverified": ["BIT-20DEC30-CDE"]}
    from coinfly.observability.diagnostics import build_identity

    desktop.state = DesktopState(freeze({**base, "automation": running, "build": dict(build_identity())}), True)
    desktop._refresh()
    assert panel.state_label.cget("text") == desktop.tr("AUTOMATION_RUNNING")
    assert "PIPELINE FIXTURE" in panel.strategy.cget("text")
    assert "PIPELINE TEST MODE" in panel.strategy.cget("text")
    assert "166700/25582938" in panel.strategy.cget("text")
    assert "manual re-enable only" in panel.strategy.cget("text")
    assert "NOT MARKET DATA" in panel.feed.cget("text")
    assert "subtype unverified (excluded): BIT-20DEC30-CDE" in panel.feed.cget("text")
    assert "NATIVE_KERNEL_UNAVAILABLE" in panel.components.cget("text")
    # Settings renders identity and provenance from the same snapshot.
    desktop.show_view("settings")
    identity = desktop.settings.identity.cget("text")
    assert "worker and desktop builds agree" in identity
    assert "166700/25582938" in identity and "NATIVE_KERNEL_UNAVAILABLE" in identity
    desktop.show_view("overview")
    assert "ETH-USD (NOT_PERMITTED)" in panel.universe.cget("text")
    assert "BUY → DISPATCHED" in panel.decision.cget("text")
    assert panel.buttons["pause_entries"].cget("state") == "normal"
    assert panel.buttons["enable"].cget("state") == "disabled"
    assert panel.inputs["preset"].cget("state") == "disabled"
    assert panel.slots.item("s1")["values"][0] == "BTC-USD"
    desktop.automation_action("pause_entries")
    assert sent[-1][0] == "automation.pause_entries"
    desktop.set_language("de")
    panel = desktop.overview.automation
    assert panel.state_label.cget("text") == "LÄUFT"
    desktop.set_language("en")


@pytest.fixture
def release_smoke(request):
    if not request.config.getoption("--native-ui"):
        pytest.skip("Native desktop smoke tests require ./dev.sh test-ui")
    script = Path(__file__).resolve().parents[2] / "scripts/verify_ui_startup.py"
    return runpy.run_path(str(script))["run_desktop_probe"]


def test_release_smoke_never_contacts_operator_services(release_smoke, monkeypatch, tmp_path):
    from coinfly.ui.ipc_client import DesktopClient

    paths = []
    original = DesktopClient._new_connection

    def connect(client):
        paths.append(client.path)
        assert client.path.parent.name.startswith("coinfly-ui-")
        assert not client.path.exists()
        return original(client)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Release smoke accessed an operator service")

    monkeypatch.setattr(DesktopClient, "_new_connection", connect)
    monkeypatch.setattr("coinfly.security.desktop_credentials.native_keyring", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    report_path = tmp_path / "result.json"
    assert release_smoke(report_path) == 0
    report = json.loads(report_path.read_text())
    assert report["main_window_ready"] is True
    assert report["event_loop_ticks"] >= 3
    assert paths
    # Reader-thread connection errors are reported to the UI, so also assert
    # isolation on the test thread rather than relying on its exception path.
    assert all(path.parent.name.startswith("coinfly-ui-") for path in paths)


def test_release_smoke_fails_on_scheduled_render_error(release_smoke, monkeypatch, tmp_path):
    def fail_refresh(_app):
        raise ValueError("fixture rendering failure")

    monkeypatch.setattr(CoinflyApp, "_refresh", fail_refresh)
    with pytest.raises(RuntimeError, match="callback failed") as failure:
        release_smoke(tmp_path / "result.json")
    assert isinstance(failure.value.__cause__, ValueError)
    assert not (tmp_path / "result.json").exists()
