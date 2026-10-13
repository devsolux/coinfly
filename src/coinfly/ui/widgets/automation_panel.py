"""Automation controls and immutable worker status: the desktop commands, never decides.

Every value shown comes from the worker's ``automation`` status block. The
panel exposes exactly the worker-owned commands (configure, enable, pause
entries, resume, stop and reconcile) and renders component readiness, the
labeled data source, universe selection reasons, the last observation and
decision with its reason, execution/reconciliation health, and the owned slots
with their display fields. A scripted pipeline fixture or a synthetic feed is
labeled as such wherever it appears.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from coinfly.ui.theme import ACCENT, MUTED, PANEL, WARNING, label, table

FEED_OPTIONS = ("COINBASE_PUBLIC_WEBSOCKET", "COINBASE_PUBLIC_REST", "REPLAY_FILE")
PRESET_OPTIONS = ("malecns-autonomous",)


class AutomationPanel:
    def __init__(self, parent: Any, app: Any) -> None:
        self.app = app
        ctk, tr = app.ctk, app.tr
        self.frame = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=14)
        self.frame.columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self.frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 4))
        label(ctk, header, tr("automation"), size=17, bold=True).pack(side="left")
        self.state_label = label(ctk, header, tr("AUTOMATION_NOT_CONFIGURED"), bold=True, color=WARNING)
        self.state_label.pack(side="right")
        controls = ctk.CTkFrame(self.frame, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=18, pady=4)
        self.inputs: dict[str, Any] = {}
        for key, caption, options, width in (("preset", "strategy_preset", list(PRESET_OPTIONS), 200),
                                             ("feed", "data_source", list(FEED_OPTIONS), 235)):
            group = ctk.CTkFrame(controls, fg_color="transparent")
            group.pack(side="left", padx=(0, 16))
            label(ctk, group, tr(caption), color=MUTED).pack(anchor="w")
            control = ctk.CTkOptionMenu(group, values=options, width=width)
            control.pack(anchor="w", pady=(3, 0))
            self.inputs[key] = control
        for key, caption, default in (("capital", "approved_capital", "100"), ("replay_name", "replay_name", "")):
            group = ctk.CTkFrame(controls, fg_color="transparent")
            group.pack(side="left", padx=(0, 16))
            label(ctk, group, tr(caption), color=MUTED).pack(anchor="w")
            entry = ctk.CTkEntry(group, width=140)
            entry.insert(0, default)
            entry.pack(anchor="w", pady=(3, 0))
            self.inputs[key] = entry
        model_controls = ctk.CTkFrame(self.frame, fg_color="transparent")
        model_controls.grid(row=2, column=0, sticky="ew", padx=18, pady=4)
        for key, caption, options, width in (
            ("controller_mode", "controller_mode", ["BASELINE_INFERENCE", "FROZEN_CHECKPOINT"], 220),
            ("max_active_slots", "max_active_slots", [str(i) for i in range(1, 9)], 75),
            ("directions", "direction_permissions", ["LONG_ONLY", "SHORT_ONLY", "BOTH"], 150),
            ("checkpoint_profile", "controller_profile", ["BASELINE", "v2", "v3"], 110),
        ):
            group = ctk.CTkFrame(model_controls, fg_color="transparent")
            group.pack(side="left", padx=(0, 16))
            label(ctk, group, tr(caption), color=MUTED).pack(anchor="w")
            widget = ctk.CTkOptionMenu(group, values=options, width=width)
            widget.pack(anchor="w", pady=(3, 0))
            self.inputs[key] = widget
        checkpoint = ctk.CTkFrame(model_controls, fg_color="transparent")
        checkpoint.pack(side="left")
        label(ctk, checkpoint, tr("checkpoint"), color=MUTED).pack(anchor="w")
        self.inputs["checkpoint_id"] = ctk.CTkEntry(checkpoint, width=190)
        self.inputs["checkpoint_id"].pack(anchor="w", pady=(3, 0))
        flags = ctk.CTkFrame(controls, fg_color="transparent")
        flags.pack(side="left", padx=(0, 16))
        self.auto_manage = ctk.CTkCheckBox(flags, text=tr("auto_manage_slots"))
        self.auto_manage.select()
        self.auto_manage.pack(anchor="w", pady=(18, 0))
        self.research_mode = ctk.CTkCheckBox(flags, text=tr("research_mode"))
        self.research_mode.pack(anchor="w", pady=(6, 0))
        actions = ctk.CTkFrame(self.frame, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="w", padx=18, pady=(4, 6))
        self.buttons: dict[str, Any] = {}
        for key, caption, color in (("configure", "configure_automation", None), ("enable", "enable_automation", ACCENT),
                                    ("pause_entries", "pause_entries", None), ("resume_entries", "resume_entries", None),
                                    ("stop", "stop", None)):
            kwargs = {"fg_color": color, "text_color": "#10271F"} if color else {}
            button = ctk.CTkButton(actions, text=tr(caption), width=170, command=lambda action=key: app.automation_action(action), **kwargs)
            button.pack(side="left", padx=(0, 12))
            self.buttons[key] = button
        self.strategy = label(ctk, self.frame, "", color=MUTED, wraplength=950)
        self.strategy.grid(row=4, column=0, sticky="ew", padx=18)
        self.feed = label(ctk, self.frame, "", color=MUTED, wraplength=950)
        self.feed.grid(row=5, column=0, sticky="ew", padx=18)
        self.components = label(ctk, self.frame, "", color=MUTED, wraplength=950)
        self.components.grid(row=6, column=0, sticky="ew", padx=18)
        self.universe = label(ctk, self.frame, "", color=MUTED, wraplength=950)
        self.universe.grid(row=7, column=0, sticky="ew", padx=18)
        self.decision = label(ctk, self.frame, "", wraplength=950)
        self.decision.grid(row=8, column=0, sticky="ew", padx=18)
        self.health = label(ctk, self.frame, "", color=MUTED, wraplength=950)
        self.health.grid(row=9, column=0, sticky="ew", padx=18)
        self.failure = label(ctk, self.frame, "", color=WARNING, wraplength=950)
        self.failure.grid(row=10, column=0, sticky="ew", padx=18)
        self.slots = table(self.frame, (("instrument", tr("instrument"), 100), ("strategy", tr("strategy"), 150),
                                        ("reason", tr("selection_reason"), 170), ("lifecycle", tr("lifecycle"), 90),
                                        ("ownership", tr("ownership"), 100), ("target", tr("target_weight"), 80),
                                        ("actual", tr("actual_quantity"), 100), ("budget", tr("allocated_budget"), 90),
                                        ("warm", tr("warm"), 70), ("decision", tr("last_decision"), 130),
                                        ("blocking", tr("blocking_reason"), 150)), height=4)
        self.slots.grid(row=11, column=0, sticky="ew", padx=16, pady=(6, 14))

    def configuration(self) -> dict[str, Any]:
        feed: dict[str, Any] = {"kind": self.inputs["feed"].get()}
        if feed["kind"] == "REPLAY_FILE":
            feed["replay_name"] = self.inputs["replay_name"].get().strip() or None
        mode = self.inputs["controller_mode"].get()
        neural: dict[str, Any] = {"mode": mode}
        if mode == "FROZEN_CHECKPOINT":
            neural["checkpoint_id"] = self.inputs["checkpoint_id"].get().strip()
            profile = self.inputs["checkpoint_profile"].get()
            if profile in {"v2", "v3"}:
                neural["expected_training_profile_id"] = f"malecns-risk-adjusted-experiment-{profile}"
        return {"preset": self.inputs["preset"].get(), "capital_budget": self.inputs["capital"].get().strip(),
                "approved_capital": self.inputs["capital"].get().strip(), "feed": feed,
                "neural": neural, "max_active_slots": self.inputs["max_active_slots"].get(),
                "directions": self.inputs["directions"].get(),
                "auto_manage_slots": bool(self.auto_manage.get()), "research_mode": bool(self.research_mode.get())}


    def _components_text(self, status: Mapping[str, Any]) -> str:
        tr = self.app.tr
        parts = []
        hints = []
        for component in status.get("components") or ():
            mark = "✓" if component.get("ready") else "✗"
            code = str(component.get("code"))
            parts.append(f"{mark} {tr(str(component.get('component')))}: {code}")
            # What the operator can do about a blocking component, when there is
            # something to do (the worker keeps retrying the rest on its own).
            hint_key = f"HINT_{code}"
            if not component.get("ready") and tr(hint_key) != hint_key and tr(hint_key) not in hints:
                hints.append(tr(hint_key))
        text = f"{tr('components')}: " + " · ".join(parts) if parts else ""
        return text + "".join(f"\n→ {hint}" for hint in hints)

    def _universe_text(self, status: Mapping[str, Any]) -> str:
        """Selected instruments, rejection counts by code, then the most informative rejections first.

        An instrument that simply has no book yet (not subscribed) says little;
        the ones rejected for a threshold (spread, size, contract feasibility)
        explain an empty universe, so they are listed before the rest.
        """
        tr = self.app.tr
        universe = status.get("universe") or {}
        if not universe:
            return ""
        text = f"{tr('universe')}: {', '.join(universe.get('selected') or ()) or '—'}"
        rejected = [reason for reason in universe.get("reasons") or () if not reason.get("eligible")]
        if not rejected:
            return text
        counts: dict[str, int] = {}
        for reason in rejected:
            for code in reason.get("codes") or ():
                counts[str(code)] = counts.get(str(code), 0) + 1
        silent = {"NO_TWO_SIDED_OBSERVATION"}
        ordered = sorted(rejected, key=lambda reason: (set(reason.get("codes") or ()) <= silent, str(reason.get("instrument"))))
        text += f" · {tr('rejected')} {len(rejected)}: " + ", ".join(
            f"{code} {count}" for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5])
        text += "\n" + "; ".join(f"{reason.get('instrument')} ({reason.get('detail') or ', '.join(reason.get('codes') or ())})"
                                 for reason in ordered[:6])
        for code in ("MIN_ORDER_ABOVE_SLOT_CAP", "MIN_ORDER_ABOVE_TURNOVER_LIMIT", "MIN_ORDER_ABOVE_ORDER_LIMIT"):
            if code in counts and not universe.get("selected"):
                text += f"\n→ {tr('HINT_' + code)}"
        return text

    def _failure_text(self, status: Mapping[str, Any]) -> str:
        tr = self.app.tr
        failure = status.get("failure") or {}
        if not failure:
            return ""
        text = (f"{tr('automation_failure')}: {failure.get('code')} · {tr('failed_stage')}: {failure.get('stage')} · "
                f"{tr('exception_chain')}: {failure.get('summary') or '—'}")
        if failure.get("origin"):
            text += f" ({failure.get('origin')})"
        action = str(failure.get("corrective_action") or "")
        if action:
            text += f"\n{tr('corrective_action')}: {tr(action)}"
        return text

    def _research_text(self, status: Mapping[str, Any]) -> str:
        """RESEARCH_LEARNING session: reward windows closed/fed/pending and the last training step."""
        tr = self.app.tr
        research = status.get("research") or {}
        if not research:
            return ""
        training = research.get("last_training") or {}
        text = (f"\n{tr('research_session')}: {str(research.get('session_id') or '—')[:8]} · {tr('reward_windows')}: "
                f"{research.get('windows_closed', 0)} · {tr('windows_fed')} {research.get('windows_fed', 0)} · "
                f"{tr('pending_windows')} {research.get('pending_windows', 0)} · {tr('horizon_exceeded')} {research.get('horizon_exceeded', 0)}")
        if training:
            text += f" · {tr('last_training')}: " + " ".join(f"{key} {value}" for key, value in list(training.items())[:4])
        return text

    def _recovery_text(self, status: Mapping[str, Any]) -> str:
        """Recovery policy, the last automatic decision and the restoration report while recovering."""
        tr = self.app.tr
        recovery = status.get("recovery") or {}
        policy = recovery.get("policy") or {}
        if not recovery:
            return ""
        if policy.get("auto_resume"):
            text = (f"\n{tr('recovery_policy')}: {tr('auto_resume')} · {tr('mandate_expires')} {policy.get('mandate_expires_at') or '—'} · "
                    f"{tr('resumptions')} ≤ {policy.get('max_resumptions', 0)}")
        else:
            text = f"\n{tr('recovery_policy')}: {tr('manual_reenable')}"
        last = recovery.get("last") or {}
        if last:
            text += f" · {tr('last_recovery')}: {last.get('reason')} ({tr('yes') if last.get('resumed') else tr('no')})"
        restoration = recovery.get("restoration") or {}
        if restoration:
            parts = []
            for key in ("infrastructure", "financial", "neural"):
                block = restoration.get(key) or {}
                parts.append(f"{'✓' if block.get('ready') else '✗'} {tr(key)}: {block.get('code', '—')}")
            text += f"\n{tr('restoration')}: " + " · ".join(parts)
        return text

    def _catalog_text(self, feed: Mapping[str, Any]) -> str:
        """Instrument catalog: what the last refresh excluded, by code; ambiguous subtypes by product id."""
        tr = self.app.tr
        catalog = feed.get("catalog") or {}
        if not catalog:
            return ""
        excluded = catalog.get("excluded") or {}
        text = f" · {tr('catalog')}: {catalog.get('entries', 0)}"
        if excluded:
            text += f" ({tr('catalog_excluded')}: " + ", ".join(f"{code} {count}" for code, count in excluded.items()) + ")"
        unverified = catalog.get("subtype_unverified") or []
        if unverified:
            text += f" · {tr('subtype_unverified')}: {', '.join(str(item) for item in unverified[:5])}"
        others = catalog.get("other_subtypes") or {}
        if others:
            text += f" · {tr('other_contract_types')}: " + ", ".join(f"{code} {count}" for code, count in sorted(others.items()))
        if catalog.get("last_error"):
            text += f" · {tr('catalog_error')}: {catalog['last_error']}"
        return text

    def update(self, state: Any) -> None:
        tr = self.app.tr
        status: Mapping[str, Any] = state.automation
        automation_state = str(status.get("state") or "NOT_CONFIGURED")
        self.state_label.configure(text=tr("AUTOMATION_" + automation_state),
                                   text_color=ACCENT if automation_state in ("RUNNING", "OBSERVING", "READY") else WARNING)
        connected = state.connected and bool(state.effective)
        enabled = bool(status.get("enabled"))
        configured = bool(status.get("configured"))
        paused = bool(status.get("entries_paused"))
        can_configure = connected and state.normal_controls_enabled and not self.app.switch_pending
        self.buttons["configure"].configure(state="normal" if can_configure else "disabled")
        self.buttons["enable"].configure(state="normal" if can_configure and configured and (not enabled or paused) else "disabled")
        self.buttons["resume_entries"].configure(state="normal" if connected and enabled and paused else "disabled")
        self.buttons["pause_entries"].configure(state="normal" if connected and enabled and not paused else "disabled")
        self.buttons["stop"].configure(state="normal" if connected and (enabled or automation_state == "PAUSED_ENTRIES") else "disabled")
        for widget in self.inputs.values():
            widget.configure(state="normal" if can_configure and not enabled else "disabled")
        strategy = status.get("strategy") or {}
        if strategy:
            fixture = strategy.get("controller_profile") == "fixture-sequence-v1"
            text = (f"{tr('strategy')}: {strategy.get('identity')} · {strategy.get('label', '')} · {tr('controller')}: "
                    f"{strategy.get('controller_profile')}{' · ' + tr('PIPELINE_FIXTURE') if fixture else ''} · "
                    f"{tr('mapping')}: {strategy.get('mapping_version')} · {tr('approved_capital')}: {tr.decimal(strategy.get('approved_capital'))} · "
                    f"{tr('research_mode')}: {tr('yes') if strategy.get('research_mode') else tr('no')} · "
                    f"{tr('learning')}: {tr('yes') if strategy.get('learning_enabled') else tr('no')}")
            if status.get("pipeline_test_mode"):
                text += f"\n{tr('PIPELINE_TEST_MODE')}"
            provenance = status.get("provenance") or {}
            if provenance:
                graph = provenance.get("graph") or {}
                kernel = provenance.get("kernel") or {}
                checkpoint = provenance.get("checkpoint") or {}
                checkpoint_text = checkpoint.get("checkpoint_id") or tr(str(checkpoint.get("kind") or "—"))
                text += (f"\n{tr('provenance')}: {tr('controller_mode')} {provenance.get('mode')} · {tr('graph')} "
                         f"{graph.get('neurons') or '—'}/{graph.get('edges') or '—'} · {tr('kernel_identity')} "
                         f"{(kernel.get('binary_sha256') or '—')[:12]} · {tr('checkpoint')} {checkpoint_text} · "
                         f"{provenance.get('feature_transform')}")
            text += self._research_text(status)
            text += self._recovery_text(status)
            if status.get("restart_reason"):
                text += f"\n{tr(str(status['restart_reason']))}"
            if status.get("reactivation_operation_id"):
                text += f"\n{tr('reactivation_pending')}: {status['reactivation_operation_id']}"
        else:
            text = tr("automation_not_configured_hint")
        self.strategy.configure(text=text)
        feed = status.get("feed") or {}
        if feed:
            self.feed.configure(text=f"{tr('data_source')}: {feed.get('label')} · {tr('feed_state')}: {feed.get('state')} · "
                                     f"{tr('last_message')}: {feed.get('last_message_at') or '—'} · {tr('sequence_gaps')}: {feed.get('sequence_gaps', 0)} · "
                                     f"{tr('reconnects')}: {feed.get('reconnects', 0)} · {tr('resyncs')}: {feed.get('resynchronizations', 0)}"
                                     + ("" if feed.get("real_market_data") else f" · {tr('NOT_MARKET_DATA')}")
                                     + self._catalog_text(feed))
        else:
            self.feed.configure(text="")
        self.components.configure(text=self._components_text(status))
        self.universe.configure(text=self._universe_text(status))
        decision = status.get("last_decision") or {}
        if decision:
            self.decision.configure(text=f"{tr('last_observation')}: {status.get('last_observation_at') or '—'} · {tr('last_decision')}: "
                                         f"{decision.get('instrument')} {decision.get('side')} → {decision.get('outcome')} "
                                         f"({tr('reason')}: {decision.get('outcome_code') or decision.get('admission_reason')}) · "
                                         f"{decision.get('decided_at')}"
                                         + (f" · {tr('PIPELINE_FIXTURE')}" if decision.get("pipeline_fixture") else ""))
        else:
            self.decision.configure(text=f"{tr('last_observation')}: {status.get('last_observation_at') or '—'} · {tr('last_decision')}: —")
        health = status.get("execution_health") or {}
        causes = health.get("intent_causes") or {}
        cause_text = " · ".join(f"{key} {value}" for key, value in causes.items() if value) if causes else ""
        self.health.configure(text=f"{tr('execution_health')}: {tr('open_intents')} {health.get('open_intents', 0)} · "
                                   f"{tr('unknown_outcomes')} {health.get('unknown', 0)} · {tr('fills')} {health.get('fills', 0)} · "
                                   f"{tr('fenced')}: {tr('yes') if health.get('fenced') else tr('no')} · {tr('ticks')}: {status.get('ticks', 0)}"
                                   + (f"\n{tr('intent_causes')}: {cause_text}" if cause_text else ""))
        self.failure.configure(text=self._failure_text(status))
        for item in self.slots.get_children():
            self.slots.delete(item)
        for slot in status.get("slots") or ():
            decision = slot.get("last_decision") or {}
            self.slots.insert("", "end", iid=str(slot.get("slot_id")), values=(
                slot.get("instrument"), slot.get("strategy"), slot.get("selection_reason") or "—", slot.get("lifecycle"),
                slot.get("ownership_state"), slot.get("target_weight"), tr.decimal(slot.get("actual_quantity"), places=8),
                tr.decimal(slot.get("allocated_budget")), f"{tr('yes') if slot.get('warm') else tr('no')} ({slot.get('observations', 0)})",
                f"{decision.get('side')} → {decision.get('outcome')}" if decision else "—", slot.get("blocking_reason") or "—"))
