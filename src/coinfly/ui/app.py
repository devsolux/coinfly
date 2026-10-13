"""coinfly 1.0.0 native desktop. All financial commands cross authenticated IPC."""

from __future__ import annotations

import importlib
import importlib.metadata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from coinfly import __application_id__, __project_name__, __version__
from coinfly.desktop_errors import DesktopDependencyError
from coinfly.i18n import Translator
from coinfly.ipc.auth import PeerIdentity
from coinfly.ipc.protocol import RPCError, freeze, thaw
from coinfly.observability.diagnostics import source_fingerprint

from .credential_worker import ConnectionVerifier, CredentialStore, CredentialWorker
from .dialogs.context_switch import ContextSwitchDialog
from .dialogs.dataset_bootstrap import DatasetBootstrapDialog
from .ipc_client import DesktopClient
from .render_scheduler import RenderEvent, RenderQueue, RenderScheduler
from .state import DesktopState, SwitchSelection
from .theme import ACCENT, BACKGROUND, DANGER, MUTED, PANEL, WARNING, configure_table_style, label
from .views.activity import ActivityView
from .views.overview import OverviewView
from .views.risk import RiskView
from .views.settings import SettingsView

CERTIFIED_CUSTOMTKINTER_VERSION = "6.0.0"


class DesktopView(Protocol):
    frame: Any

    def update(self) -> None: ...


def load_toolkit() -> Any:
    try:
        version = importlib.metadata.version("customtkinter")
    except importlib.metadata.PackageNotFoundError as exc:
        raise DesktopDependencyError("coinfly desktop requires the pinned CustomTkinter 6.0.0 build. "
                                     "Install the certified desktop dependency set; headless commands remain available.") from exc
    if version != CERTIFIED_CUSTOMTKINTER_VERSION:
        raise DesktopDependencyError(f"CustomTkinter {version} is not the pinned coinfly build. "
                                     "CustomTkinter 6.0.0 and native platform qualification are required.")
    try:
        return importlib.import_module("customtkinter")
    except ImportError as exc:
        raise DesktopDependencyError("The native Tk/CustomTkinter desktop runtime is unavailable.") from exc


class CoinflyApp:
    def __init__(self, toolkit: Any, socket_path: str | Path | None = None, *,
                 peer_verifier: Callable[[PeerIdentity], bool] | None = None,
                 worker_launcher: Callable[[], None] | None = None,
                 credential_store: CredentialStore | None = None,
                 connection_verifier: ConnectionVerifier | None = None) -> None:
        self.ctk = toolkit
        self.tr = Translator("en")
        # Computed once for this desktop process; compared with the worker's.
        self.local_fingerprint = source_fingerprint()
        self.state = DesktopState(local_source_fingerprint=self.local_fingerprint)
        self.current_view = "overview"
        self.switch_pending = False
        self._callbacks: dict[str, Callable[[Mapping[str, Any]], None]] = {}
        self._last_mutation_request: str | None = None
        self._bootstrap: DatasetBootstrapDialog | None = None
        self._switch_dialog: ContextSwitchDialog | None = None
        self._closing = False
        toolkit.set_appearance_mode("dark")
        toolkit.set_default_color_theme("dark-blue")
        self.root = toolkit.CTk()
        self.root.title(f"{__project_name__} · {__version__}")
        self.root.geometry("1420x960")
        self.root.minsize(1160, 820)
        self.root.configure(fg_color=BACKGROUND)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        configure_table_style()
        self.queue = RenderQueue(capacity=64)
        self.credential_worker = CredentialWorker(self.queue, credential_store, connection_verifier)
        self.credential_status = "CREDENTIALS_UNCHECKED"
        # Remote authentication is a separate state from local storage; neither
        # implies the other and neither enables live execution.
        self.connection_status = "COINBASE_AUTH_UNCHECKED"
        self.connection_http_status: int | None = None
        self.connection_test_available = connection_verifier is not None
        self.client = DesktopClient(self.queue, socket_path, peer_verifier=peer_verifier,
                                    worker_launcher=worker_launcher)
        self._build()
        self.scheduler = RenderScheduler(self.root, self.queue, self._render)
        self.scheduler.start()
        self.client.start()
        self.root.after(200, self._refresh)

    def _build(self) -> None:
        ctk, tr = self.ctk, self.tr
        self.shell = ctk.CTkFrame(self.root, fg_color=BACKGROUND, corner_radius=0)
        self.shell.pack(fill="both", expand=True)
        self.shell.columnconfigure(1, weight=1)
        self.shell.rowconfigure(1, weight=1)
        sidebar = ctk.CTkFrame(self.shell, fg_color=PANEL, width=192, corner_radius=0)
        sidebar.grid(row=0, column=0, rowspan=3, sticky="nsew")
        sidebar.grid_propagate(False)
        label(ctk, sidebar, "coinfly", size=31, color=ACCENT, bold=True).pack(anchor="w", padx=24, pady=(28, 0))
        label(ctk, sidebar, "1.0.0", color=MUTED).pack(anchor="w", padx=26, pady=(0, 36))
        self.nav: dict[str, Any] = {}
        for view in ("overview", "activity", "risk", "settings"):
            button = ctk.CTkButton(sidebar, text=tr(view), width=160, height=43, anchor="w",
                                    fg_color="transparent", command=lambda name=view: self.show_view(name))
            button.pack(padx=16, pady=5)
            self.nav[view] = button
        language = ctk.CTkOptionMenu(sidebar, values=["EN", "DE"], width=148, command=lambda value: self.set_language(value.lower()))
        language.set(tr.language.upper())
        language.pack(side="bottom", padx=22, pady=24)
        label(ctk, sidebar, __application_id__, size=10, color=MUTED).pack(side="bottom", padx=14, pady=4)
        header = ctk.CTkFrame(self.shell, fg_color=BACKGROUND)
        header.grid(row=0, column=1, sticky="ew", padx=24, pady=(20, 10))
        self.connection = label(ctk, header, tr("offline"), color=WARNING, bold=True)
        self.connection.pack(side="left")
        self.safety_button = ctk.CTkButton(header, text=tr("safety"), command=lambda: self.show_view("risk"))
        self.safety_button.pack(side="right", padx=(12, 0))
        self.pause_button = ctk.CTkButton(header, text=tr("pause_entries"), state="disabled",
                                         command=lambda: self.safety_action("safety.pause_entries"))
        self.pause_button.pack(side="right")
        self.setup_button = ctk.CTkButton(header, text=tr("bootstrap"), command=self.open_setup)
        self.setup_button.pack(side="right", padx=12)
        self.host = ctk.CTkFrame(self.shell, fg_color="transparent")
        self.host.grid(row=1, column=1, sticky="nsew", padx=24, pady=8)
        self.host.columnconfigure(0, weight=1)
        self.host.rowconfigure(0, weight=1)
        self.overview = OverviewView(self.host, self)
        self.activity = ActivityView(self.host, self)
        self.risk = RiskView(self.host, self)
        self.settings = SettingsView(self.host, self)
        self.views: dict[str, DesktopView] = {"overview": self.overview, "activity": self.activity,
                                             "risk": self.risk, "settings": self.settings}
        for panel in self.views.values():
            panel.frame.grid(row=0, column=0, sticky="nsew")
        footer = ctk.CTkFrame(self.shell, fg_color="transparent")
        footer.grid(row=2, column=1, sticky="ew", padx=24, pady=(4, 12))
        self.footer = label(ctk, footer, "—", size=11, color=MUTED)
        self.footer.pack(side="left")
        self.status = label(ctk, footer, "", size=11, color=MUTED, wraplength=750)
        self.status.pack(side="right")
        self.show_view(self.current_view)

    def set_language(self, language: str) -> None:
        if language == self.tr.language:
            return
        self.tr = Translator(language)
        if self._bootstrap is not None:
            self._bootstrap.close()
            self._bootstrap = None
        if self._switch_dialog is not None:
            self._switch_dialog.window.destroy()
            self._switch_dialog = None
        self.shell.destroy()
        self._build()
        self._refresh()

    def show_view(self, name: str) -> None:
        if self._bootstrap is not None:
            self._bootstrap.window.withdraw()
        self.current_view = name
        # CTkScrollableFrame.lift raises its outer container; Tk's inherited
        # tkraise only raises the content inside its canvas, leaving it covered.
        self.views[name].frame.lift()
        for key, button in self.nav.items():
            button.configure(fg_color="#2A4D4A" if key == name else "transparent")
        self.views[name].update()

    def show_status(self, message: str, *, error: bool = False) -> None:
        self.status.configure(text=message, text_color=DANGER if error else MUTED)

    def send(self, method: str, params: Mapping[str, Any], *,
             callback: Callable[[Mapping[str, Any]], None] | None = None, quiet: bool = False) -> str | None:
        if callback is not None and len(self._callbacks) >= 64:
            self.show_status("COMMAND_QUEUE_FULL", error=True)
            return None
        try:
            request_id = self.client.submit(method, params, epoch=self.state.epoch)
        except RPCError as exc:
            self.show_status(exc.code, error=True)
            return None
        if callback is not None:
            self._callbacks[request_id] = callback
        if method in {"context.apply", "engine.arm", "slot.create", "slot.start", "slot.edit", "slot.stop", "slot.delete", "safety.stop", "operation.retry",
                      "automation.configure", "automation.enable", "automation.stop"}:
            self._last_mutation_request = request_id
        if not quiet:
            self.show_status(self.tr("processing"))
        return request_id

    def _render(self, event: RenderEvent) -> None:
        payload = event.payload
        if event.kind == "snapshot":
            self.state = DesktopState(freeze(payload), self.state.connected, self.local_fingerprint)
            self._refresh()
        elif event.kind == "connection":
            snapshot = self.state.snapshot
            if payload.get("connected") is not True and payload.get("timed_out"):
                snapshot = freeze({**snapshot, "bootstrap": {"stage": "BLOCKED", "ready": False,
                                  "component": "database", "reason": "WORKER_START_UNAVAILABLE"}})
            self.state = DesktopState(snapshot, payload.get("connected") is True, self.local_fingerprint)
            self._refresh()
        elif event.kind == "credentials":
            self.credential_status = str(payload["code"])
            self._refresh()
        elif event.kind == "connection_test":
            self.connection_status = str(payload.get("code", "COINBASE_AUTH_UNKNOWN_FAILURE"))
            status = payload.get("http_status")
            self.connection_http_status = status if isinstance(status, int) and not isinstance(status, bool) else None
            self._refresh()
        elif event.kind == "bootstrap_progress":
            snapshot = thaw(self.state.snapshot)
            snapshot["bootstrap"] = thaw(payload)
            self.state = DesktopState(freeze(snapshot), self.state.connected, self.local_fingerprint)
            self._refresh()
        elif event.kind == "response":
            method, request_id = payload.get("method"), str(payload.get("request_id", ""))
            callback = self._callbacks.pop(request_id, None)
            if method in {"context.preview", "context.apply"}:
                self.switch_pending = False
            if "error" in payload:
                code = str(payload["error"])
                if code == "RESPONSE_UNAVAILABLE":
                    message = f"{code} · {self.tr('reconcile_request')} ({request_id})"
                elif code == "WORKER_BUILD_MISMATCH":
                    # The worker refuses commands from a desktop built from other sources: say how to replace it.
                    message = f"{code} — {self.tr('WORKER_BUILD_MISMATCH')}"
                else:
                    message = code
                self.show_status(message, error=True)
            else:
                result = payload.get("result")
                if callback is not None and isinstance(result, Mapping):
                    callback(result)
                elif isinstance(result, Mapping) and result.get("operation_id"):
                    self.show_status(f"{self.tr('operation')}: {result['operation_id']} · {result.get('state', '')}")
                elif method == "history.export" and isinstance(result, Mapping):
                    self.show_status(f"{result.get('state', 'EXPORTED')} · {result.get('records', 0)}")
                elif not str(method).startswith("history."):
                    self.show_status(str(result.get("state", "ACCEPTED")) if isinstance(result, Mapping) else "ACCEPTED")
            self._refresh()
        elif event.kind == "resnapshot_required":
            self.state = DesktopState(self.state.snapshot, False, self.local_fingerprint)
            self._callbacks.clear()
            self.switch_pending = False
            self.show_status(str(payload.get("code", "RESNAPSHOT_REQUIRED")), error=True)
            self.client.request_resnapshot()
            self._refresh()
        elif event.kind == "alarm":
            self.show_status(str(payload.get("code", "ACTIVE_ALARM")), error=True)
        elif event.kind == "history_changed":
            self.activity.changed.configure(text=self.tr("new_events"))

    def _refresh(self) -> None:
        if self._closing:
            return
        local = self.state.snapshot.get("bootstrap", {}).get("verification") == "LOCAL_SOURCE_LOCKS"
        self.root.title("coinfly · 1.0.0" + (" · " + self.tr("local_development") if local else ""))
        caption = self.state.effective_caption if self.state.effective else self.tr("no_effective_context")
        if self.state.worker_source_mismatch:
            caption = f"{caption} · {self.tr('WORKER_SOURCE_MISMATCH')}"
        self.connection.configure(text=caption if self.state.connected else self.tr("offline"),
                                  text_color=ACCENT if self.state.ready and not self.state.worker_source_mismatch else WARNING)
        self.pause_button.configure(state="normal" if self.state.safety_controls_enabled else "disabled")
        self.setup_button.configure(state="disabled" if self.state.ready else "normal")
        self.footer.configure(text=(self.tr("local_development") + " · " if local else "") +
                              f"epoch {self.state.epoch} · config v{self.state.revision} · ledger #{self.state.snapshot.get('ledger_sequence', self.state.snapshot.get('sequence', 0))}")
        self.views[self.current_view].update()
        if not self.state.ready:
            if self._bootstrap is None:
                self._bootstrap = DatasetBootstrapDialog(self)
                # A terminal prerequisite failure is an inline status, not a
                # window covering the history/settings the user came to use.
                if self.state.snapshot.get("bootstrap", {}).get("stage") in {"BLOCKED", "CANCELLED"}:
                    self._bootstrap.window.withdraw()
            self._bootstrap.update(self.state.snapshot.get("bootstrap", {}))
        elif self._bootstrap is not None:
            self._bootstrap.close()
            self._bootstrap = None

    def open_setup(self) -> None:
        if self.state.ready:
            return
        if self._bootstrap is None:
            self._bootstrap = DatasetBootstrapDialog(self)
        self._bootstrap.update(self.state.snapshot.get("bootstrap", {}))
        self._bootstrap.window.deiconify()
        self._bootstrap.window.lift()

    def preview_context(self) -> None:
        if not self.state.normal_controls_enabled or self.switch_pending:
            return
        try:
            selection = SwitchSelection(**self.overview.selection())
            candidate = selection.candidate(self.state.effective or self.state.proposed)
        except RPCError as exc:
            self.show_status(exc.code, error=True)
            return
        self.preview_config(candidate)

    def preview_config(self, candidate: Mapping[str, Any]) -> None:
        """Every GUI financial change uses the same immutable preview workflow."""
        if not self.state.context_controls_enabled or self.switch_pending:
            return
        revision, epoch = self.state.revision, self.state.epoch
        if self.state.effective and candidate == thaw(self.state.effective):
            self.show_status(self.tr("unchanged"))
            return
        self.switch_pending = True
        request_id = self.send("context.preview", {"config": candidate, "expected_revision": revision, "expected_epoch": epoch},
                  callback=lambda preview: self._preview_ready(preview, revision, epoch, candidate))
        if request_id is None:
            self.switch_pending = False
        self._refresh()

    def _preview_ready(self, preview: Mapping[str, Any], revision: int, epoch: int, candidate: Mapping[str, Any]) -> None:
        if self.state.revision != revision or self.state.epoch != epoch:
            self.show_status("PREVIEW_REVISION_STALE", error=True)
            return
        display = dict(preview)
        display.setdefault("target", candidate)
        self._switch_dialog = ContextSwitchDialog(self, display, lambda: self._apply_preview(preview, revision, epoch))

    def _apply_preview(self, preview: Mapping[str, Any], revision: int, epoch: int) -> None:
        if not self.state.normal_controls_enabled or self.switch_pending or (self.state.revision, self.state.epoch) != (revision, epoch):
            self.show_status("PREVIEW_REVISION_STALE", error=True)
            return
        self.switch_pending = True
        params = {"preview_id": preview.get("preview_id"), "config_digest": preview.get("config_digest"),
                  "expected_revision": revision, "expected_epoch": epoch,
                  "drain_policy": preview.get("drain_policy", "BOUNDED_RISK"), "confirmation": True}
        if self.send("context.apply", params) is None:
            self.switch_pending = False
        self._refresh()

    def safety_action(self, method: str) -> None:
        if self.state.safety_controls_enabled:
            self.send(method, {"expected_revision": self.state.revision, "expected_epoch": self.state.epoch})

    def inspect_last_request(self) -> None:
        if self._last_mutation_request:
            self.send("operation.get", {"request_id": self._last_mutation_request})

    def restart_stale_worker(self) -> None:
        """Retire a worker whose code no longer matches this desktop, then relaunch."""
        if not self.state.connected:
            return
        self.client.restart_worker(epoch=self.state.epoch)
        self.show_status(self.tr("worker_restart_requested"))

    def retry_operation(self) -> None:
        transition = self.state.snapshot.get("transition") or {}
        operation_id = transition.get("operation_id")
        if not operation_id:
            slot_id = self.overview.selected_slot()
            slot: Mapping[str, Any] = next((item for item in self.state.snapshot.get("slots", ()) if str(item.get("slot_id", item.get("id"))) == slot_id), {})
            operation_id = slot.get("operation_id")
        if operation_id:
            self.send("operation.retry", {"operation_id": operation_id, "expected_revision": self.state.revision,
                                           "expected_epoch": self.state.epoch})

    def confirm_arm(self) -> None:
        if not self.state.normal_controls_enabled:
            return
        revision, epoch = self.state.revision, self.state.epoch
        def arm() -> None:
            self.send("engine.arm", {"expected_revision": revision, "expected_epoch": epoch, "confirmation": True})
        self._confirmation(self.tr("arm_title"), f"{self.state.effective_caption}\n\n{self.tr('arm_note')}",
                           arm)

    def _confirmation(self, title: str, message: str, action: Callable[[], None]) -> None:
        window = self.ctk.CTkToplevel(self.root)
        window.title(title)
        window.geometry("670x330")
        window.transient(self.root)
        label(self.ctk, window, title, size=21, bold=True).pack(anchor="w", padx=24, pady=(22, 14))
        label(self.ctk, window, message, wraplength=620).pack(anchor="w", padx=24, pady=12)
        def confirm() -> None:
            window.destroy()
            action()
        self.ctk.CTkButton(window, text=self.tr("confirm"), command=confirm).pack(side="right", padx=24, pady=24)
        self.ctk.CTkButton(window, text=self.tr("cancel"), command=window.destroy).pack(side="left", padx=24, pady=24)

    def automation_action(self, action: str) -> None:
        """Worker-owned automation commands; the desktop only sends revision-bound requests."""
        if not self.state.connected:
            return
        revisions = {"expected_revision": self.state.revision, "expected_epoch": self.state.epoch}
        if action == "configure":
            self.send("automation.configure", self.overview.automation.configuration())
        elif action == "enable":
            def enable() -> None:
                self.send("automation.enable", revisions)
            self._confirmation(self.tr("enable_automation"), f"{self.state.effective_caption}\n\n{self.tr('enable_automation_note')}", enable)
        elif action == "resume_entries":
            self.send("automation.resume_entries", revisions)
        elif action == "pause_entries":
            self.send("automation.pause_entries", {})
        elif action == "stop":
            def stop() -> None:
                self.send("automation.stop", {})
            self._confirmation(self.tr("stop"), f"{self.state.effective_caption}\n\n{self.tr('stop_automation_note')}", stop)

    def slot_action(self, action: str) -> None:
        slot_id = self.overview.selected_slot()
        if action == "new":
            self._new_slot()
        elif action == "edit" and slot_id:
            self._edit_slot(slot_id)
        elif slot_id:
            method = "slot." + action
            slot: Mapping[str, Any] = next((item for item in self.state.snapshot.get("slots", ()) if str(item.get("slot_id", item.get("id"))) == slot_id), {})
            self.send(method, {"slot_id": slot_id, "expected_revision": slot.get("revision", 0),
                               "expected_epoch": (slot.get("context") or {}).get("epoch", self.state.epoch)})

    def _edit_slot(self, slot_id: str) -> None:
        if not self.state.normal_controls_enabled:
            return
        slot = next((item for item in self.state.snapshot.get("slots", ()) if str(item.get("slot_id", item.get("id"))) == slot_id), None)
        if slot is None:
            return
        epoch = self.state.epoch
        window = self.ctk.CTkToplevel(self.root)
        window.title(self.tr("edit"))
        window.geometry("650x300")
        window.transient(self.root)
        label(self.ctk, window, self.tr("instrument"), size=18, bold=True).pack(anchor="w", padx=24, pady=(22, 12))
        instrument = self.ctk.CTkEntry(window, width=590)
        instrument.insert(0, str(slot.get("instrument", "")))
        instrument.pack(padx=24, pady=8)
        label(self.ctk, window, self.tr("switch_note"), wraplength=590, color=WARNING).pack(anchor="w", padx=24, pady=8)
        def edit() -> None:
            value = instrument.get().strip().upper()
            if not value or len(value) > 80:
                return
            window.destroy()
            self.send("slot.edit", {"slot_id": slot_id, "changes": {"instrument": value}, "expected_revision": slot.get("revision", 0),
                                    "expected_epoch": (slot.get("context") or {}).get("epoch", epoch)})
        self.ctk.CTkButton(window, text=self.tr("confirm"), command=edit).pack(side="right", padx=24, pady=18)

    def _new_slot(self) -> None:
        if not self.state.normal_controls_enabled:
            return
        revision, epoch = self.state.revision, self.state.epoch
        window = self.ctk.CTkToplevel(self.root)
        window.title(self.tr("new"))
        window.geometry("500x230")
        window.transient(self.root)
        label(self.ctk, window, self.tr("instrument"), size=18, bold=True).pack(anchor="w", padx=24, pady=22)
        instrument = self.ctk.CTkEntry(window, width=440, placeholder_text="BTC-USD")
        instrument.pack(padx=24, pady=4)
        def create() -> None:
            value = instrument.get().strip().upper()
            if not value or len(value) > 80:
                return
            window.destroy()
            self.send("slot.create", {"instrument": value, "expected_revision": revision, "expected_epoch": epoch})
        self.ctk.CTkButton(window, text=self.tr("confirm"), command=create).pack(side="right", padx=24, pady=20)

    @property
    def credentials_busy(self) -> bool:
        # Derived from the single-flight worker, so a dropped render event can
        # never leave the credential controls disabled forever.
        return self.credential_worker.busy

    def store_credentials(self, key: str, secret: str) -> bool:
        if not self.credential_worker.submit(key, secret):
            return False
        self.credential_status = "KEYRING_WORKING"
        return True

    def check_credentials(self) -> None:
        if self.credential_worker.submit():
            self.credential_status = "KEYRING_WORKING"
            self._refresh()

    def test_connection(self) -> None:
        """Explicit user action: one bounded, read-only authenticated Coinbase request."""
        if self.credential_worker.submit_connection_test():
            self.connection_status = "COINBASE_AUTH_CHECKING"
            self.connection_http_status = None
            self._refresh()

    def close(self) -> None:
        self._closing = True
        self.scheduler.stop()
        self.client.close()
        self.credential_worker.close()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def run_app(socket_path: str | Path | None = None, *,
            peer_verifier: Callable[[PeerIdentity], bool] | None = None,
            worker_launcher: Callable[[], None] | None = None,
            credential_store: CredentialStore | None = None,
            connection_verifier: ConnectionVerifier | None = None) -> int:
    toolkit = load_toolkit()
    from tkinter import TclError
    try:
        app = CoinflyApp(toolkit, socket_path, peer_verifier=peer_verifier, worker_launcher=worker_launcher,
                         credential_store=credential_store, connection_verifier=connection_verifier)
    except TclError as exc:
        raise DesktopDependencyError("Native desktop display is unavailable.") from exc
    app.run()
    return 0
