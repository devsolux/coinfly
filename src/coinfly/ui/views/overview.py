"""Requested/effective context and guarded slot controls."""

from typing import Any

from coinfly.ui.state import default_subtype_label
from coinfly.ui.theme import ACCENT, MUTED, PANEL, WARNING, label, table
from coinfly.ui.widgets.automation_panel import AutomationPanel
from coinfly.ui.widgets.balance_coverage import BalanceCoverage

SUBTYPE_OPTIONS = {"SPOT": ("SPOT",),
                   "PERPETUAL": ("PERPETUAL_STYLE_FUTURE", "PERPETUAL_NO_EXPIRY", "DATED_FUTURE")}


class OverviewView:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.frame.columnconfigure(0, weight=1)
        config = ctk.CTkFrame(self.frame, fg_color=PANEL, corner_radius=14)
        config.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        config.columnconfigure(0, weight=1)
        label(ctk, config, tr("requested"), size=17, bold=True).grid(row=0, column=0, sticky="w", padx=18, pady=(14, 6))
        selectors = ctk.CTkFrame(config, fg_color="transparent")
        selectors.grid(row=1, column=0, sticky="ew", padx=18, pady=6)
        self.selectors: dict[str, Any] = {}
        for key, caption, options in (("product_track", "mode", ["SPOT", "PERPETUAL"]),
                                       ("product_subtype", "contract_model", list(SUBTYPE_OPTIONS["SPOT"])),
                                       ("environment", "environment", ["SANDBOX", "LIVE"]),
                                       ("region_policy", "region", ["GERMANY", "GLOBAL"])):
            group = ctk.CTkFrame(selectors, fg_color="transparent")
            group.pack(side="left", padx=(0, 18))
            label(ctk, group, tr(caption), color=MUTED).pack(anchor="w")
            control = ctk.CTkOptionMenu(group, values=options, width=155 if key != "product_subtype" else 215,
                                        command=self._selection_changed)
            control.pack(anchor="w", pady=(3, 0))
            self.selectors[key] = control
        self.preview = ctk.CTkButton(selectors, text=tr("apply"), fg_color=ACCENT, text_color="#10271F",
                                    command=app.preview_context)
        self.preview.pack(side="right", anchor="s")
        self.effective = label(ctk, config, tr("offline"), bold=True)
        self.effective.grid(row=2, column=0, sticky="w", padx=18, pady=(8, 0))
        self.capability = label(ctk, config, "", color=MUTED, wraplength=950)
        self.capability.grid(row=3, column=0, sticky="ew", padx=18)
        self.transition = label(ctk, config, "", color=WARNING, wraplength=950)
        self.transition.grid(row=4, column=0, sticky="ew", padx=18)
        actions = ctk.CTkFrame(config, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="w", padx=18, pady=(2, 0))
        self.retry = ctk.CTkButton(actions, text=tr("retry_operation"), width=180, command=app.retry_operation)
        self.retry.pack(side="left")
        self.restart_worker = ctk.CTkButton(actions, text=tr("restart_worker"), width=180, command=app.restart_stale_worker)
        self.restart_worker.pack(side="left", padx=(12, 0))
        label(ctk, config, tr("global_policy"), color=MUTED, wraplength=950).grid(row=6, column=0, sticky="ew", padx=18, pady=(4, 14))
        self.balance = BalanceCoverage(self.frame, app)
        self.balance.frame.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self.automation = AutomationPanel(self.frame, app)
        self.automation.frame.grid(row=2, column=0, sticky="ew", pady=(0, 14))
        slots = ctk.CTkFrame(self.frame, fg_color=PANEL, corner_radius=14)
        slots.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        toolbar = ctk.CTkFrame(slots, fg_color="transparent")
        toolbar.pack(fill="x", padx=16, pady=12)
        label(ctk, toolbar, tr("slots"), size=17, bold=True).pack(side="left")
        self.buttons: dict[str, Any] = {}
        for key in ("new", "start", "edit", "stop", "delete"):
            button = ctk.CTkButton(toolbar, text=tr(key), width=88, command=lambda action=key: app.slot_action(action))
            button.pack(side="left", padx=(12, 0))
            self.buttons[key] = button
        self.slots = table(slots, (("instrument", tr("instrument"), 120), ("track", tr("mode"), 90),
                                   ("lifecycle", tr("lifecycle"), 140), ("execution", tr("execution"), 140),
                                   ("protection", tr("protection"), 280)), height=6)
        self.slots.pack(fill="x", padx=16, pady=(0, 14))
        self._loaded_revision: int | None = None

    def _selection_changed(self, _: str) -> None:
        # No policy rule here: every combination is previewable and the worker's
        # capability resolver explains the outcome. Only the contract-model list
        # follows the selected track.
        track = self.selectors["product_track"].get()
        options = list(SUBTYPE_OPTIONS[track])
        subtype = self.selectors["product_subtype"]
        subtype.configure(values=options)
        if subtype.get() not in options:
            subtype.set(default_subtype_label(track, self.selectors["region_policy"].get()))

    def selection(self) -> dict[str, str]:
        return {key: value.get() for key, value in self.selectors.items()}

    def _capability_text(self, state: Any) -> str:
        tr = self.app.tr
        selected = self.selection()
        for row in state.snapshot.get("capabilities", ()):
            if (row.get("environment"), row.get("region_policy"), row.get("product_track")) == (
                    selected["environment"], selected["region_policy"], selected["product_track"]) \
                    and row.get("product_subtype") == selected["product_subtype"]:
                break
        else:
            return ""
        category = str(row.get("category", "UNKNOWN"))
        text = f"{tr('capability')}: {tr('CAPABILITY_' + category)} · {tr('route')}: {row.get('route') or '—'}"
        codes = row.get("codes") or ()
        if codes:
            text += " · " + ", ".join(str(code) for code in codes)
        return text + "\n" + str(row.get("explanation", ""))

    def selected_slot(self) -> str | None:
        selection = self.slots.selection()
        return selection[0] if selection else None

    def update(self) -> None:
        state, tr = self.app.state, self.app.tr
        if self._loaded_revision != state.revision and (state.effective or state.proposed):
            proposed = state.proposed or state.effective
            for key, widget in self.selectors.items():
                widget.set(str(proposed.get(key, state.effective.get(key, widget.get()))))
            self._loaded_revision = state.revision
            self._selection_changed("")
        enabled = state.normal_controls_enabled and not self.app.switch_pending
        for widget in self.selectors.values():
            widget.configure(state="normal" if enabled else "disabled")
        self.preview.configure(state="normal" if enabled else "disabled")
        self.effective.configure(text=f"{tr('effective')}: {state.effective_caption}")
        self.capability.configure(text=self._capability_text(state))
        transition = state.snapshot.get("transition") or {}
        transition_text = f"{tr('operation')} {transition.get('operation_id', '')} · {tr(str(transition.get('state', transition.get('phase', ''))))}" if transition else ""
        failure = state.transition_failure
        if failure is not None:
            transition_text += (f"\n{tr('failed_stage')}: {tr(failure['stage'])} · {tr('error_code')}: {failure['code']}"
                                f" · {tr('retry_safe' if failure['retry_safe'] else 'retry_unsafe')}"
                                f"\n{tr('corrective_action')}: {tr(failure['corrective_action'])}")
            if failure["summary"]:
                transition_text += f"\n{tr('exception_chain')}: {failure['summary']}"
        if state.worker_source_mismatch:
            transition_text += ("\n" if transition_text else "") + tr("WORKER_SOURCE_MISMATCH_DETAIL")
        self.transition.configure(text=transition_text)
        self.retry.configure(state="normal" if state.safety_controls_enabled and state.transition_retryable else "disabled")
        self.restart_worker.configure(state="normal" if state.connected and state.worker_source_mismatch else "disabled")
        self.balance.update(state.snapshot.get("balance", {}))
        self.automation.update(state)
        for key, button in self.buttons.items():
            allowed = state.safety_controls_enabled if key in ("stop", "delete") else enabled
            button.configure(state="normal" if allowed else "disabled")
        previous = self.selected_slot()
        for item in self.slots.get_children():
            self.slots.delete(item)
        for slot in state.snapshot.get("slots", ())[:100]:
            identifier = str(slot.get("slot_id", slot.get("id", "")))
            if not identifier:
                continue
            self.slots.insert("", "end", iid=identifier, values=(slot.get("instrument", "—"), slot.get("product_track", slot.get("track", "—")),
                              slot.get("lifecycle", slot.get("state", "UNKNOWN")), slot.get("execution_state", "—"),
                              slot.get("operation_id", slot.get("protection", "—"))))
        if previous and self.slots.exists(previous):
            self.slots.selection_set(previous)
