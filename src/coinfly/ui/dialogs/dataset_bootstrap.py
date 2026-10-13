"""Dataset setup blocks activation but never takes away safety access."""

from collections.abc import Mapping
from typing import Any

from coinfly.ui.theme import ACCENT, MUTED, PANEL, WARNING, label


class DatasetBootstrapDialog:
    def __init__(self, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.window = ctk.CTkToplevel(app.root)
        self.window.title(tr("bootstrap"))
        self.window.geometry("870x610")
        self.window.transient(app.root)
        self.window.configure(fg_color=PANEL)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.heading = label(ctk, self.window, tr("bootstrap"), size=25, bold=True)
        self.heading.pack(anchor="w", padx=24, pady=(24, 12))
        self._indeterminate = False
        self.stage = label(ctk, self.window, tr("checking"), size=17, color=ACCENT, bold=True)
        self.stage.pack(anchor="w", padx=24, pady=12)
        self.progress = ctk.CTkProgressBar(self.window, width=800, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x", padx=24, pady=12)
        self.details = label(ctk, self.window, "", color=MUTED, wraplength=810)
        self.details.pack(anchor="w", padx=24, pady=6)
        self.evidence = label(ctk, self.window, "", color=WARNING, wraplength=810)
        self.evidence.pack(anchor="w", padx=24, pady=6)
        self.trust = label(ctk, self.window, tr("bootstrap_trust"), color=MUTED, wraplength=810)
        self.trust.pack(anchor="w", padx=24, pady=12)
        label(ctk, self.window, tr("bootstrap_note"), wraplength=810).pack(anchor="w", padx=24, pady=8)
        controls = ctk.CTkFrame(self.window, fg_color="transparent")
        controls.pack(fill="x", padx=24, pady=14)
        self.buttons: list[Any] = []
        for caption, method in (("pause", "bootstrap.pause"), ("retry", "bootstrap.retry"), ("cancel_setup", "bootstrap.cancel")):
            button = ctk.CTkButton(controls, text=tr(caption), width=180,
                                    command=lambda value=method: app.send(value, {}))
            button.pack(side="left", padx=(0, 12))
            self.buttons.append(button)
        ctk.CTkButton(controls, text=tr("network_settings"), width=180, command=self.open_settings).pack(side="left")
        ctk.CTkButton(self.window, text=tr("open_safety"), command=self.open_safety).pack(anchor="w", padx=24, pady=10)
        ctk.CTkButton(self.window, text=tr("continue_history"), command=self.open_history).pack(anchor="w", padx=24)

    def update(self, bootstrap: Mapping[str, Any]) -> None:
        tr = self.app.tr
        database = bootstrap.get("component") == "database"
        title = tr("database_bootstrap" if database else "bootstrap")
        self.window.title(title)
        self.heading.configure(text=title)
        self.trust.configure(text=tr("bootstrap_local_trust" if bootstrap.get("verification") == "LOCAL_SOURCE_LOCKS"
                                     else "bootstrap_trust"))
        received, expected = bootstrap.get("received_bytes", bootstrap.get("bytes_completed", 0)), bootstrap.get("expected_bytes", bootstrap.get("bytes_total", 0))
        progress = min(1.0, max(0.0, received / expected)) if isinstance(received, (int, float)) and isinstance(expected, (int, float)) and expected > 0 else 0.0
        active = bootstrap.get("stage") in {"CHECKING", "RETRYING", "DOWNLOADING", "VERIFYING", "PREPARING"}
        indeterminate = active and not expected
        if indeterminate != self._indeterminate:
            self.progress.stop()
            self.progress.configure(mode="indeterminate" if indeterminate else "determinate")
            if indeterminate:
                self.progress.start()
            self._indeterminate = indeterminate
        if not indeterminate:
            self.progress.set(progress)
        self.stage.configure(text=f"{tr('stage')}: {bootstrap.get('stage', 'CHECKING')}")
        self.details.configure(text=f"{bootstrap.get('asset') or 'MaleCNS'} · {tr('bytes')}: {received} / {expected or '—'}\n"
                                    f"{tr('throughput')}: {bootstrap.get('bytes_per_second', '—')} B/s  ·  "
                                    f"{tr('retries')}: {bootstrap.get('retries', 0)}  ·  {tr('disk')}: {bootstrap.get('disk_status', 'UNKNOWN')}")
        evidence = bootstrap.get("missing_evidence", ())
        reason = (", ".join(tr(str(item)) for item in evidence)
                  or bootstrap.get("detail") or tr(str(bootstrap.get("reason") or "")))
        self.evidence.configure(text=f"{tr('evidence')}: {reason}")
        paused = bootstrap.get("stage") == "PAUSED"
        self.buttons[1].configure(text=tr("resume" if paused else "retry"))
        for index, button in enumerate(self.buttons):
            enabled = self.app.state.connected and (
                index == 1 and not active or not database and (index == 0 and active or index == 2 and (active or paused)))
            button.configure(state="normal" if enabled else "disabled")

    def open_safety(self) -> None:
        self.window.withdraw()
        self.app.show_view("risk")

    def open_settings(self) -> None:
        self.window.withdraw()
        self.app.show_view("settings")

    def open_history(self) -> None:
        self.window.withdraw()
        self.app.show_view("activity")

    def close(self) -> None:
        if self.window.winfo_exists():
            self.progress.stop()
            self.window.destroy()
