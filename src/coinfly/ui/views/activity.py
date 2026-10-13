"""On-demand frozen history; new telemetry never moves an open report page."""

from collections.abc import Mapping
from typing import Any

from coinfly.ui.theme import ACCENT, MUTED, PANEL, WARNING, label, table


class ActivityView:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        self.page_data: Mapping[str, Any] | None = None
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.columnconfigure(0, weight=1)
        top = ctk.CTkFrame(self.frame, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", pady=(4, 16))
        label(ctk, top, tr("activity"), size=28, bold=True).pack(side="left")
        self.environment = ctk.CTkOptionMenu(top, values=["SANDBOX", "LIVE"], width=130)
        self.environment.pack(side="right", padx=10)
        self.mode = ctk.CTkOptionMenu(top, values=[tr("all_events"), tr("trade_results")], width=180)
        self.mode.pack(side="right", padx=10)
        self.totals = label(ctk, self.frame, "—", size=20, color=ACCENT, bold=True)
        self.totals.grid(row=1, column=0, sticky="w", pady=(0, 12))
        actions = ctk.CTkFrame(self.frame, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.search = ctk.CTkEntry(actions, placeholder_text=tr("search"), width=250)
        self.search.pack(side="left")
        self.refresh = ctk.CTkButton(actions, text=tr("refresh"), command=self.open)
        self.refresh.pack(side="left", padx=12)
        self.export = ctk.CTkButton(actions, text=tr("export"), width=120, state="disabled", command=self.export_csv)
        self.export.pack(side="left", padx=(0, 12))
        self.changed = label(ctk, actions, "", color=WARNING, wraplength=500)
        self.changed.pack(side="left")
        panel = ctk.CTkFrame(self.frame, fg_color=PANEL)
        panel.grid(row=3, column=0, sticky="nsew")
        self.rows = table(panel, (("time", tr("time"), 180), ("instrument", tr("instrument"), 100),
                                 ("event", tr("event"), 210), ("status", tr("status"), 150),
                                 ("net", tr("net"), 110)), height=10)
        self.rows.pack(fill="both", expand=True, padx=14, pady=14)
        self.pagination = label(ctk, self.frame, tr("report_empty"), color=MUTED)
        self.pagination.grid(row=4, column=0, sticky="w", pady=12)
        navigation = ctk.CTkFrame(self.frame, fg_color="transparent")
        navigation.grid(row=5, column=0, sticky="w")
        self.buttons: dict[str, Any] = {}
        for direction in ("first", "previous", "next", "last"):
            button = ctk.CTkButton(navigation, text=tr(direction), width=110, state="disabled",
                                    command=lambda value=direction: self.navigate(value))
            button.pack(side="left", padx=(0, 10))
            self.buttons[direction] = button
        label(ctk, self.frame, tr("history_frozen"), color=MUTED, wraplength=950).grid(row=6, column=0, sticky="w", pady=16)

    def open(self) -> None:
        if not self.app.state.connected:
            return
        if self.page_data:
            self.app.send("history.close", {"snapshot_id": self.page_data["snapshot_id"]}, quiet=True)
        self.changed.configure(text="")
        self.app.send("history.open", {"authority_id": self.app.state.effective.get("authority_id", "local-paper"),
                                      "environment": self.environment.get(),
                                      "mode": "TRADE_RESULTS" if self.mode.get() == self.app.tr("trade_results") else "ALL_EVENTS",
                                      "search": self.search.get()[:120]}, callback=self.display)

    def navigate(self, direction: str) -> None:
        if not self.page_data:
            return
        cursor = self.page_data.get("previous_cursor" if direction == "previous" else "next_cursor")
        self.app.send("history.page", {"snapshot_id": self.page_data["snapshot_id"], "direction": direction,
                                      "cursor": cursor if direction in ("previous", "next") else None}, callback=self.display)

    def export_csv(self) -> None:
        if not self.page_data or not self.app.state.connected:
            return
        from pathlib import Path
        from tkinter.filedialog import asksaveasfilename

        from coinfly.ipc.protocol import RPCError
        destination = asksaveasfilename(parent=self.app.root, title=self.app.tr("export"), defaultextension=".csv",
                                       initialfile=f"coinfly-{self.page_data['environment'].lower()}-report.csv",
                                       filetypes=[("CSV", "*.csv")])
        if destination:
            try:
                self.app.client.export_history(self.page_data["snapshot_id"], Path(destination), epoch=self.app.state.epoch)
                self.app.show_status(self.app.tr("processing"))
            except RPCError as exc:
                self.app.show_status(exc.code, error=True)

    def display(self, result: Mapping[str, Any]) -> None:
        if len(result.get("rows", ())) > 10 or result.get("page_size") != 10 or result.get("frozen") is not True:
            self.app.show_status("INVALID_HISTORY_PAGE", error=True)
            return
        self.page_data = result
        self.export.configure(state="normal" if self.app.state.connected else "disabled")
        tr = self.app.tr
        for item in self.rows.get_children():
            self.rows.delete(item)
        for row in result.get("rows", ()):
            self.rows.insert("", "end", values=(tr.timestamp(row.get("occurred_at", "")), row.get("instrument") or "—",
                             row.get("kind", ""), row.get("status", ""), tr.decimal(row.get("closed_net"))))
        totals = result.get("totals", {})
        realm = tr("simulated") if result.get("environment") == "SANDBOX" else "LIVE"
        self.totals.configure(text=f"{realm}  |  {tr('profit')} {tr.decimal(totals.get('profit'))}    "
                                  f"{tr('loss')} {tr.decimal(totals.get('loss'))}    {tr('net')} {tr.decimal(totals.get('net'))}")
        self.pagination.configure(text=f"{result.get('first_row', 0)}–{result.get('last_row', 0)} / {result.get('total_count', 0)}  ·  "
                                       f"{result.get('page_number', 1)} / {result.get('page_count', 1)}  ·  "
                                       f"Snapshot #{result.get('ledger_sequence', 0)}  ·  {tr('pending_charges')}: {totals.get('pending_charges', 0)}")
        for direction, button in self.buttons.items():
            available = result.get("previous_cursor") if direction in ("first", "previous") else result.get("next_cursor")
            button.configure(state="normal" if available and self.app.state.connected else "disabled")

    def update(self) -> None:
        self.refresh.configure(state="normal" if self.app.state.connected else "disabled")
        self.export.configure(state="normal" if self.app.state.connected and self.page_data else "disabled")
        if self.page_data and self.app.state.snapshot.get("ledger_sequence", self.app.state.snapshot.get("sequence", 0)) > self.page_data["ledger_sequence"]:
            self.changed.configure(text=self.app.tr("new_events"))
