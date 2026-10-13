"""Safety stays reachable while setup or handover blocks normal interaction."""

from typing import Any

from coinfly.ui.theme import MUTED, PANEL, WARNING, label, table


class RiskView:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        label(ctk, self.frame, tr("risk"), size=28, bold=True).pack(anchor="w", pady=(8, 18))
        self.status = label(ctk, self.frame, "—", size=17, color=WARNING, wraplength=950)
        self.status.pack(anchor="w", pady=(0, 16))
        controls = ctk.CTkFrame(self.frame, fg_color=PANEL)
        controls.pack(fill="x", pady=(0, 18))
        self.pause = ctk.CTkButton(controls, text=tr("pause_entries"), command=lambda: app.safety_action("safety.pause_entries"))
        self.pause.pack(side="left", padx=16, pady=16)
        self.stop = ctk.CTkButton(controls, text=tr("stop"), command=lambda: app.safety_action("safety.stop"))
        self.stop.pack(side="left", padx=8, pady=16)
        self.arm = ctk.CTkButton(controls, text=tr("arm"), command=app.confirm_arm)
        self.arm.pack(side="right", padx=16, pady=16)
        operations = ctk.CTkFrame(self.frame, fg_color="transparent")
        operations.pack(fill="x", pady=(0, 18))
        self.retry = ctk.CTkButton(operations, text=tr("retry_operation"), command=app.retry_operation)
        self.retry.pack(side="left")
        self.inspect = ctk.CTkButton(operations, text=tr("request_status"), command=app.inspect_last_request)
        self.inspect.pack(side="left", padx=12)
        label(ctk, self.frame, tr("alarms"), size=18, bold=True).pack(anchor="w", pady=(0, 12))
        self.alarms = table(self.frame, (("code", tr("event"), 230), ("state", tr("status"), 150),
                                        ("reason", tr("evidence"), 470)), height=10)
        self.alarms.pack(fill="x")
        self.acknowledge = ctk.CTkButton(self.frame, text=tr("acknowledge"), command=self._acknowledge)
        self.acknowledge.pack(anchor="w", pady=(14, 8))
        label(ctk, self.frame, tr("ack_note"), color=MUTED, wraplength=950).pack(anchor="w")

    def _acknowledge(self) -> None:
        selection = self.alarms.selection()
        if selection:
            self.app.send("alarm.acknowledge", {"alarm_id": selection[0], "expected_revision": self.app.state.revision,
                                                "expected_epoch": self.app.state.epoch})

    def update(self) -> None:
        state, tr = self.app.state, self.app.tr
        self.status.configure(text=f"{state.effective_caption} · {state.snapshot.get('engine_state', 'UNAVAILABLE')} · "
                                   f"{tr('keystore')}: {state.snapshot.get('keystore', state.snapshot.get('keystore_state', 'UNVERIFIED'))}")
        for button in (self.pause, self.stop, self.acknowledge, self.retry, self.inspect):
            button.configure(state="normal" if state.safety_controls_enabled else "disabled")
        self.arm.configure(state="normal" if state.normal_controls_enabled and not self.app.switch_pending else "disabled")
        selected = self.alarms.selection()
        for item in self.alarms.get_children():
            self.alarms.delete(item)
        for index, alarm in enumerate(state.snapshot.get("alarms", ())[:100]):
            identifier = str(alarm.get("alarm_id", alarm.get("id", index)))
            self.alarms.insert("", "end", iid=identifier, values=(alarm.get("code", alarm.get("kind", alarm.get("reason", "UNKNOWN"))),
                               alarm.get("state", "ACKNOWLEDGED / FENCED" if alarm.get("acknowledged") else "ACTIVE / FENCED"),
                               alarm.get("reason", alarm.get("message", ""))))
        if selected and self.alarms.exists(selected[0]):
            self.alarms.selection_set(selected[0])
