"""Honest scoped account valuation with separate spendable funds."""

from collections.abc import Mapping
from typing import Any

from coinfly.ui.state import balance_presentation
from coinfly.ui.theme import ACCENT, MUTED, PANEL, WARNING, label


class BalanceCoverage:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=14)
        self.frame.columnconfigure((0, 1), weight=1)
        label(ctk, self.frame, tr("balance"), size=22, bold=True).grid(row=0, column=0, sticky="w", padx=20, pady=(16, 0))
        self.realm = label(ctk, self.frame, "—", color=WARNING, bold=True)
        self.realm.grid(row=0, column=1, sticky="e", padx=20, pady=(16, 0))
        self.amount = label(ctk, self.frame, "—", size=38, color=ACCENT, bold=True)
        self.amount.grid(row=1, column=0, sticky="w", padx=20, pady=4)
        self.scope = label(ctk, self.frame, tr("subtotal"), color=WARNING)
        self.scope.grid(row=1, column=1, sticky="e", padx=20)
        self.coverage = label(ctk, self.frame, tr("no_funds"), color=MUTED, wraplength=900)
        self.coverage.grid(row=2, column=0, columnspan=2, sticky="ew", padx=20, pady=(0, 12))
        self.values: dict[str, Any] = {}
        fields = (("settled", "settled_assets"), ("unsettled", "unsettled_perpetual_pnl"),
                  ("locked", "locked_execution_collateral"), ("restricted", "vault_restrictions"),
                  ("spendable", "active_book_spendable"), ("discrepancy", "discrepancy"))
        for index, (title, key) in enumerate(fields):
            frame = ctk.CTkFrame(self.frame, fg_color="transparent")
            frame.grid(row=3 + index // 2, column=index % 2, sticky="ew", padx=20, pady=4)
            label(ctk, frame, tr(title), color=MUTED).pack(side="left")
            value = label(ctk, frame, "—", bold=True)
            value.pack(side="right")
            self.values[key] = value
        label(ctk, self.frame, tr("balance_note"), color=MUTED, wraplength=900).grid(
            row=6, column=0, columnspan=2, sticky="ew", padx=20, pady=(12, 16))

    def update(self, balance: Mapping[str, Any]) -> None:
        tr = self.app.tr
        dto = balance_presentation(balance)
        self.realm.configure(text=tr("simulated") if dto["realm"] == "SIMULATED" else dto["realm"])
        currency = str(balance.get("reporting_currency", ""))
        amount = balance.get("equity", balance.get("observed_equity", balance.get("total")))
        self.amount.configure(text=f"{tr.decimal(amount)} {currency}".strip())
        self.scope.configure(text=tr("verified_scope" if dto["scope_label"] == "VERIFIED_SCOPE" else "subtotal"))
        included, expected = balance.get("included_scopes", "?"), balance.get("expected_scopes", "?")
        if isinstance(included, (list, tuple)):
            included = len(included)
        if isinstance(expected, (list, tuple)):
            expected = len(expected)
        self.coverage.configure(text=f"{tr('coverage')}: {dto['coverage']} · {included} / {expected} | "
                                     f"{tr('account_age')}: {balance.get('account_age_seconds', balance.get('observation_age_seconds', '—'))} s | "
                                     f"{tr('mark_age')}: {balance.get('mark_age_seconds', '—')} s")
        aliases = {"active_book_spendable": "active_book_available", "unsettled_perpetual_pnl": "unsettled_perpetual_upl"}
        for key, widget in self.values.items():
            widget.configure(text=tr.decimal(balance.get(key, balance.get(aliases.get(key, key)))))
