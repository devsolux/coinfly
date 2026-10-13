"""One explicit preview confirmation; no optimistic effective-context changes."""

from collections.abc import Callable, Mapping
from typing import Any

from coinfly.ui.theme import MUTED, PANEL, WARNING, label


class ContextSwitchDialog:
    def __init__(self, app: Any, preview: Mapping[str, Any], on_confirm: Callable[[], None]) -> None:
        self.app, self.preview, self.on_confirm = app, preview, on_confirm
        ctk, tr = app.ctk, app.tr
        self.window = ctk.CTkToplevel(app.root)
        self.window.title(tr("switch_title"))
        self.window.geometry("820x660")
        self.window.transient(app.root)
        self.window.configure(fg_color=PANEL)
        label(ctk, self.window, tr("switch_title"), size=24, bold=True).pack(anchor="w", padx=24, pady=(22, 16))
        source = preview.get("source", preview.get("source_context", app.state.effective))
        target = preview.get("target", preview.get("candidate", preview.get("config", preview.get("target_config", {}))))
        def context_caption(context: Any) -> str:
            if isinstance(context, Mapping):
                config = context.get("config", context) if isinstance(context.get("config"), Mapping) else context
                parts = [str(config.get(key, "—")) for key in ("environment", "product_track")]
                subtype = config.get("product_subtype")
                parts.append(tr(str(subtype)) if subtype else tr("default_contract_model"))
                parts.append(str(config.get("region_policy", "—")))
                parts.append(str(config.get("broker_profile_id", "—")))
                return " / ".join(parts)
            return str(context)
        label(ctk, self.window, f"{tr('source')}: {context_caption(source)}", bold=True, wraplength=765).pack(anchor="w", padx=24, pady=4)
        label(ctk, self.window, f"{tr('target')}: {context_caption(target)}", bold=True, wraplength=765).pack(anchor="w", padx=24, pady=4)
        if isinstance(target, Mapping):
            label(ctk, self.window, f"{tr('scope')}: {', '.join(target.get('execution_scope', ()))}", color=MUTED,
                  wraplength=765).pack(anchor="w", padx=24, pady=4)
        fields = (("affected", "affected_slots"), ("outstanding", "outstanding_orders"),
                  ("drain_policy", "drain_policy"), ("fees_slippage", "fees_slippage"), ("target_capital", "target_funded_capital"))
        for caption, key in fields:
            value = preview.get(key, tr("unknown"))
            label(ctk, self.window, f"{tr(caption)}: {value}", color=MUTED, wraplength=765).pack(anchor="w", padx=24, pady=5)
        label(ctk, self.window, tr("switch_note"), wraplength=765).pack(anchor="w", padx=24, pady=12)
        if app.state.effective.get("environment") == "LIVE":
            label(ctk, self.window, tr("live_drain"), color=WARNING, bold=True, wraplength=765).pack(anchor="w", padx=24, pady=10)
        controls = ctk.CTkFrame(self.window, fg_color="transparent")
        controls.pack(side="bottom", fill="x", padx=24, pady=24)
        ctk.CTkButton(controls, text=tr("cancel"), command=self.window.destroy).pack(side="left")
        self.confirm = ctk.CTkButton(controls, text=tr("confirm"), command=self._confirm)
        self.confirm.pack(side="right")
        if not app.state.ready or preview.get("bootstrap_ready") is False:
            self.confirm.configure(state="disabled")
            label(ctk, self.window, tr("dataset_activation_required"), color=WARNING,
                  wraplength=765).pack(anchor="w", padx=24, pady=8)

    def _confirm(self) -> None:
        self.confirm.configure(state="disabled")
        self.window.destroy()
        self.on_confirm()
