"""Scannable execution settings and asynchronous OS-keyring credential setup."""

from collections.abc import Mapping
from decimal import InvalidOperation
from tkinter import StringVar
from typing import Any

from coinfly import __application_id__, __project_name__, __version__
from coinfly.config.credential_input import credential_input_error
from coinfly.config.models import RiskLimits
from coinfly.ipc.protocol import RPCError, thaw
from coinfly.observability.diagnostics import build_identity
from coinfly.ui.state import SwitchSelection
from coinfly.ui.theme import ACCENT, DANGER, MUTED, PANEL, WARNING, label


class SettingsView:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        label(ctk, self.frame, tr("settings"), size=28, bold=True).pack(anchor="w", pady=(8, 18))
        language = ctk.CTkFrame(self.frame, fg_color=PANEL)
        language.pack(fill="x", pady=(0, 16))
        label(ctk, language, tr("language"), size=16, bold=True).pack(side="left", padx=18, pady=18)
        self.language = ctk.CTkOptionMenu(language, values=["English", "Deutsch"], command=self._language)
        self.language.set("Deutsch" if tr.language == "de" else "English")
        self.language.pack(side="right", padx=18, pady=18)
        perimeter = ctk.CTkFrame(self.frame, fg_color=PANEL)
        perimeter.pack(fill="x", pady=(0, 16))
        label(ctk, perimeter, tr("environment"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 6))
        self.environment = ctk.CTkSegmentedButton(perimeter,
            values=[tr("paper_environment"), tr("live_environment")], command=self._environment)
        self.environment.set(tr("paper_environment"))
        self.environment.pack(fill="x", padx=18, pady=8)
        self.environment_status = label(ctk, perimeter, "", color=WARNING, wraplength=650)
        self.environment_status.pack(anchor="w", padx=18, pady=(4, 8))
        label(ctk, perimeter, tr("switch_note"), color=MUTED, wraplength=650).pack(anchor="w", padx=18, pady=(0, 16))
        profile = ctk.CTkFrame(self.frame, fg_color=PANEL)
        profile.pack(fill="x", pady=(0, 16))
        label(ctk, profile, tr("region_entity"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 6))
        self.entity = label(ctk, profile, tr("unknown"), color=MUTED, wraplength=920)
        self.entity.pack(anchor="w", padx=18, pady=(0, 16))
        vault = ctk.CTkFrame(self.frame, fg_color=PANEL)
        vault.pack(fill="x", pady=(0, 16))
        label(ctk, vault, tr("credentials"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 6))
        self.vault_status = label(ctk, vault, tr("CREDENTIALS_UNCHECKED"), color=WARNING, wraplength=650, bold=True)
        self.vault_status.pack(anchor="w", padx=18, pady=(0, 10))
        label(ctk, vault, tr("credential_storage_note"), color=MUTED, wraplength=650).pack(anchor="w", padx=18, pady=(0, 10))
        self.key_value, self.secret_value = StringVar(master=app.root), StringVar(master=app.root)
        label(ctk, vault, tr("api_key"), bold=True).pack(anchor="w", padx=18)
        self.api_key = ctk.CTkEntry(vault, textvariable=self.key_value, show="•", height=38)
        self.api_key.pack(fill="x", padx=18, pady=(4, 8))
        label(ctk, vault, tr("api_secret"), bold=True).pack(anchor="w", padx=18)
        secret_row = ctk.CTkFrame(vault, fg_color="transparent")
        secret_row.pack(fill="x", padx=18, pady=(4, 8))
        secret_row.columnconfigure(0, weight=1)
        self.api_secret = ctk.CTkEntry(secret_row, textvariable=self.secret_value, show="•", height=38)
        self.api_secret.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.secret_visible = False
        self.visibility = ctk.CTkButton(secret_row, text=tr("show_secret"), width=90, command=self._visibility)
        self.visibility.grid(row=0, column=1)
        # Preserve PEM line breaks when pasting into a single-line masked entry.
        self.api_secret.bind("<<Paste>>", self._paste_secret)
        label(ctk, vault, tr("credential_format_note"), color=MUTED, wraplength=650).pack(anchor="w", padx=18)
        self.validation = label(ctk, vault, "", color=WARNING, wraplength=650)
        self.validation.pack(anchor="w", padx=18, pady=8)
        actions = ctk.CTkFrame(vault, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 16))
        self.enroll = ctk.CTkButton(actions, text=tr("save_credentials"), state="disabled", command=self._save)
        self.enroll.pack(side="left", padx=(0, 12))
        self.check_vault = ctk.CTkButton(actions, text=tr("check_credentials"), command=app.check_credentials)
        self.check_vault.pack(side="left")
        self.key_value.trace_add("write", self._validate_credentials)
        self.secret_value.trace_add("write", self._validate_credentials)
        # Remote authentication is a separate, explicit action and a separate
        # status line. It never runs merely because Settings was opened.
        connection = ctk.CTkFrame(vault, fg_color="transparent")
        connection.pack(fill="x", padx=18, pady=(0, 16))
        self.connection_status = label(ctk, connection, tr("COINBASE_AUTH_UNCHECKED"), color=MUTED, wraplength=650, bold=True)
        self.connection_status.pack(anchor="w", pady=(0, 6))
        label(ctk, connection, tr("connection_test_note"), color=MUTED, wraplength=650).pack(anchor="w", pady=(0, 8))
        self.test_connection = ctk.CTkButton(connection, text=tr("test_connection"), command=app.test_connection,
                                             state="normal" if app.connection_test_available else "disabled")
        self.test_connection.pack(anchor="w")
        readiness = ctk.CTkFrame(self.frame, fg_color=PANEL)
        readiness.pack(fill="x", pady=(0, 16))
        label(ctk, readiness, tr("release_gates"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 6))
        self.gates = label(ctk, readiness, tr("checking"), color=MUTED, wraplength=920)
        self.gates.pack(anchor="w", padx=18, pady=(0, 16))
        # Worker identity and neural provenance, rendered from the same state
        # snapshot the Overview uses: the desktop never derives them locally.
        identity = ctk.CTkFrame(self.frame, fg_color=PANEL)
        identity.pack(fill="x", pady=(0, 16))
        label(ctk, identity, tr("worker_identity"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 6))
        self.identity = label(ctk, identity, tr("no_worker_snapshot"), color=MUTED, wraplength=920)
        self.identity.pack(anchor="w", padx=18, pady=(0, 16))
        limits = ctk.CTkFrame(self.frame, fg_color=PANEL)
        limits.pack(fill="x", pady=(0, 16))
        label(ctk, limits, tr("risk_limits"), size=17, bold=True).pack(anchor="w", padx=18, pady=(16, 10))
        self.limit_inputs: dict[str, Any] = {}
        for key in ("max_order_notional", "max_gross_exposure", "max_daily_loss", "max_drawdown_fraction", "max_active_episodes"):
            row = ctk.CTkFrame(limits, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=5)
            label(ctk, row, tr(key), color=MUTED).pack(side="left")
            entry = ctk.CTkEntry(row, width=180)
            entry.pack(side="right")
            self.limit_inputs[key] = entry
            entry.bind("<KeyRelease>", self._validate_limits)
        self.limit_validation = label(ctk, limits, "", color=WARNING, wraplength=650)
        self.limit_validation.pack(anchor="w", padx=18, pady=8)
        self.limit_preview = ctk.CTkButton(limits, text=tr("apply"), command=self._preview_limits)
        self.limit_preview.pack(anchor="w", padx=18, pady=(10, 16))
        self._limits_revision: int | None = None
        label(ctk, self.frame, f"{__project_name__} {__version__} · {__application_id__}",
              color=MUTED).pack(anchor="w", pady=18)

    def _identity_text(self, snapshot: Mapping[str, Any]) -> tuple[str, bool]:
        """Worker build vs desktop build, retirement, and the automation's provenance/readiness lines."""
        tr = self.app.tr
        worker = snapshot.get("build")
        if not isinstance(worker, Mapping):
            return tr("no_worker_snapshot"), False
        desktop = build_identity()
        same = all(worker.get(key) == desktop.get(key) for key in ("application_id", "version", "protocol_version", "source_fingerprint"))
        lines = [f"{tr('worker_build')}: {worker.get('application_id')} {worker.get('version')} · {tr('protocol')} {worker.get('protocol_version')} · "
                 f"{str(worker.get('source_fingerprint') or '—')[:12]} · Python {worker.get('python', '—')}",
                 f"{tr('desktop_build')}: {desktop['application_id']} {desktop['version']} · {tr('protocol')} {desktop['protocol_version']} · "
                 f"{str(desktop['source_fingerprint'])[:12]} · Python {desktop['python']}",
                 tr("build_match") if same else tr("build_mismatch")]
        retirement = snapshot.get("retirement")
        if isinstance(retirement, Mapping) and retirement:
            lines.append(f"{tr('retirement')}: " + " · ".join(f"{key} {value}" for key, value in retirement.items() if value is not None))
        automation = snapshot.get("automation") or {}
        provenance = automation.get("provenance") or {}
        if provenance:
            graph, kernel, checkpoint = (provenance.get(key) or {} for key in ("graph", "kernel", "checkpoint"))
            lines.append(f"{tr('provenance')}: {tr('controller_mode')} {provenance.get('mode')} · {tr('graph_identity')} "
                         f"{graph.get('neurons') or '—'}/{graph.get('edges') or '—'} · {tr('kernel_identity')} "
                         f"{(kernel.get('binary_sha256') or '—')[:12]} · {tr('checkpoint')} "
                         f"{checkpoint.get('checkpoint_id') or tr(str(checkpoint.get('kind') or '—'))} · {provenance.get('feature_transform')}")
        if automation.get("pipeline_test_mode"):
            lines.append(tr("PIPELINE_TEST_MODE"))
        components = automation.get("components") or ()
        if components:
            lines.append(f"{tr('neural_readiness')}: " + " · ".join(
                f"{'✓' if item.get('ready') else '✗'} {tr(str(item.get('component')))}: {item.get('code')}" for item in components))
        return "\n".join(lines), not same

    def _language(self, selected: str) -> None:
        self.app.set_language("de" if selected == "Deutsch" else "en")

    def _environment(self, selected: str) -> None:
        environment = "LIVE" if selected == self.app.tr("live_environment") else "SANDBOX"
        config = self.app.state.effective or self.app.state.proposed
        try:
            candidate = SwitchSelection(str(config.get("product_track", "SPOT")), environment,
                                        str(config.get("region_policy", "GERMANY"))).candidate(config)
            self.app.preview_config(candidate)
        except RPCError as error:
            self.app.show_status(self.app.tr(error.code), error=True)
        # Selection is a proposal. Never paint it as the effective realm early.
        self.update()

    def _visibility(self) -> None:
        self.secret_visible = not self.secret_visible
        self.api_secret.configure(show="" if self.secret_visible else "•")
        self.visibility.configure(text=self.app.tr("hide_secret" if self.secret_visible else "show_secret"))

    def _paste_secret(self, _event: Any) -> str:
        from tkinter import TclError
        try:
            value = self.app.root.clipboard_get()
        except TclError:
            return "break"
        if len(value) <= 4096:
            self.secret_value.set(value.strip().replace("\r\n", "\n").replace("\n", "\\n"))
        return "break"

    def _validate_credentials(self, *_: Any) -> None:
        key, secret = self.key_value.get(), self.secret_value.get()
        code = credential_input_error(key, secret)
        # Empty replacement fields are intentional after save/check. They do
        # not invalidate the separately verified keychain pair.
        if not key and not secret:
            message = self.app.credential_status
            color = ACCENT if message == "SECURELY_STORED" else MUTED if message == "CREDENTIALS_UNCHECKED" else WARNING
        else:
            message = code or "CREDENTIAL_FORMAT_VALID"
            color = WARNING if code else ACCENT
        self.validation.configure(text=self.app.tr(message), text_color=color)
        self.enroll.configure(state="normal" if code is None and not self.app.credentials_busy else "disabled")

    def _save(self) -> None:
        key, secret = self.key_value.get(), self.secret_value.get()
        if credential_input_error(key, secret) is not None:
            self._validate_credentials()
            return
        if self.app.store_credentials(key, secret):
            self.key_value.set("")
            self.secret_value.set("")
            self.secret_visible = True
            self._visibility()
            self.update()

    def _preview_limits(self) -> None:
        values = self._limit_values()
        if values is None:
            self.app.show_status(self.app.tr("invalid_risk_limits"), error=True)
            return
        candidate = thaw(self.app.state.effective or self.app.state.proposed)
        candidate["limits"] = values
        self.app.preview_config(candidate)

    def _limit_values(self) -> dict[str, Any] | None:
        try:
            if any(len(entry.get()) > 100 for entry in self.limit_inputs.values()):
                return None
            values: dict[str, Any] = {key: str(self.app.tr.parse_decimal(entry.get()))
                                      for key, entry in self.limit_inputs.items() if key != "max_active_episodes"}
            count = self.limit_inputs["max_active_episodes"].get().strip()
            if not count.isdecimal():
                raise ValueError("Positive episode count required")
            values["max_active_episodes"] = int(count)
            return RiskLimits.model_validate(values).model_dump(mode="json")
        except (InvalidOperation, ValueError):
            return None

    def _validate_limits(self, *_: Any) -> None:
        valid = self._limit_values() is not None
        self.limit_validation.configure(text="" if valid else self.app.tr("invalid_risk_limits"))
        self.limit_preview.configure(state="normal" if valid and self.app.state.normal_controls_enabled
                                     and not self.app.switch_pending else "disabled")

    def update(self) -> None:
        snapshot, tr = self.app.state.snapshot, self.app.tr
        profile = snapshot.get("verified_profile", {}) or {}
        self.entity.configure(text=str(profile.get("service_entity", snapshot.get("service_entity", tr("unknown")))))
        code = self.app.credential_status
        self.vault_status.configure(text=tr(code), text_color=ACCENT if code == "SECURELY_STORED" else
                                    WARNING if code in {"KEYRING_WORKING", "CREDENTIALS_UNCHECKED", "CREDENTIALS_NOT_STORED"}
                                    else DANGER)
        self._validate_credentials()
        self.check_vault.configure(state="disabled" if self.app.credentials_busy else "normal")
        for field in (self.api_key, self.api_secret):
            field.configure(state="disabled" if self.app.credentials_busy else "normal")
        connection = self.app.connection_status
        detail = tr(connection)
        if self.app.connection_http_status is not None:
            detail = f"{detail} (HTTP {self.app.connection_http_status})"
        self.connection_status.configure(text=detail, text_color=ACCENT if connection == "COINBASE_AUTH_VERIFIED" else
                                         MUTED if connection in {"COINBASE_AUTH_UNCHECKED", "COINBASE_AUTH_CHECKING"} else DANGER)
        self.test_connection.configure(state="normal" if self.app.connection_test_available
                                       and not self.app.credentials_busy else "disabled")
        effective = self.app.state.effective.get("environment")
        selected = effective or self.app.state.proposed.get("environment", "SANDBOX")
        self.environment.set(tr("live_environment" if selected == "LIVE" else "paper_environment"))
        self.environment.configure(state="normal" if self.app.state.context_controls_enabled and not self.app.switch_pending else "disabled")
        failure = self.app.state.transition_failure
        if failure is not None:
            status = (f"{tr(str(failure['phase']))} · {tr('failed_stage')}: {tr(failure['stage'])} · "
                      f"{tr('error_code')}: {failure['code']}\n{tr('corrective_action')}: {tr(failure['corrective_action'])}")
        elif self.app.state.switching:
            status = tr(str(self.app.state.transition_phase))
        elif not self.app.state.ready:
            status = tr("dataset_activation_required")
        elif selected == "LIVE" and effective != "LIVE":
            status = tr("LIVE_ENROLLMENT_REQUIRED")
        elif effective:
            status = tr("paper_context_ready") if effective == "SANDBOX" else tr("effective")
        else:
            status = tr("paper_context_not_initialized")
        if self.app.state.worker_source_mismatch:
            status += "\n" + tr("WORKER_SOURCE_MISMATCH_DETAIL")
        self.environment_status.configure(text=f"{tr('effective')}: {effective or tr('no_effective_context')}\n{status}")
        bootstrap = snapshot.get("bootstrap", {})
        codes = snapshot.get("release_gates", bootstrap.get("missing_evidence", ()))
        if isinstance(codes, Mapping):
            codes = [f"{key}: {value}" for key, value in codes.items()]
        blocked = [f"{item['code']}: {item.get('detail', '')}" if isinstance(item, Mapping) else str(item)
                   for item in codes if not isinstance(item, Mapping) or not item.get("satisfied")]
        if bootstrap.get("reason"):
            blocked.insert(0, str(bootstrap.get("detail") or tr(str(bootstrap["reason"]))))
        self.gates.configure(text=(tr("missing_prerequisites") + "\n" + "\n".join(blocked)
                                   + "\n\n" + tr("research_access_note")) if blocked else tr("checking"),
                             text_color=WARNING if blocked else MUTED)
        identity_text, identity_warning = self._identity_text(snapshot)
        self.identity.configure(text=identity_text, text_color=WARNING if identity_warning else MUTED)
        if self._limits_revision != self.app.state.revision:
            limits = (self.app.state.effective or self.app.state.proposed).get("limits", {})
            for key, entry in self.limit_inputs.items():
                value = str(limits.get(key, ""))
                if tr.language == "de" and key != "max_active_episodes":
                    value = value.replace(".", ",")
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, value)
            self._limits_revision = self.app.state.revision
        state = "normal" if self.app.state.normal_controls_enabled and not self.app.switch_pending else "disabled"
        for entry in self.limit_inputs.values():
            entry.configure(state=state)
        self.limit_preview.configure(state=state)
        self._validate_limits()
