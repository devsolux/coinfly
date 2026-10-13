"""Why an order intent exists: a decoded decision, a protective drain, or an operator action.

The execution ledger records *what* was ordered; this table records *why*. Every
intent the automation dispatches for a decoded decision is labeled with the
decision it traces to, so an entry or strategy exit can always be traced to a
genuine decoded decision (or to the pipeline fixture that produced it, which is
labeled as such). Protective drains (context or slot drain, operator stop) and
risk actions are labeled separately and never counted as strategy trades.

An intent without a row here is reported as ``UNLABELED``; nothing infers a
cause from the intent's shape.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal

from coinfly.storage.component_schema import install_component_schema

INTENT_CAUSES_SCHEMA_COMPONENT = "intent_causes"
INTENT_CAUSES_SCHEMA: tuple[str, ...] = (
    'CREATE TABLE IF NOT EXISTS cf_intent_causes (intent_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, cause TEXT NOT NULL, origin TEXT NOT NULL, decision_id TEXT, controller_profile TEXT, pipeline_fixture INTEGER NOT NULL DEFAULT 0, recorded_at TEXT NOT NULL)',
    'CREATE INDEX cf_intent_causes_context ON cf_intent_causes(context_id, cause)',
)

IntentCause = Literal["DECODED_DECISION_ENTRY", "DECODED_DECISION_EXIT", "PROTECTIVE_DRAIN", "OPERATOR_ACTION"]
CAUSES: tuple[str, ...] = ("DECODED_DECISION_ENTRY", "DECODED_DECISION_EXIT", "PROTECTIVE_DRAIN", "OPERATOR_ACTION")


def ensure_schema(connection: sqlite3.Connection) -> None:
    install_component_schema(connection, INTENT_CAUSES_SCHEMA_COMPONENT, INTENT_CAUSES_SCHEMA)


def record_cause_tx(connection: sqlite3.Connection, *, intent_id: str, context_id: str, cause: str, origin: str,
                    decision_id: str | None = None, controller_profile: str | None = None, pipeline_fixture: bool = False,
                    now: datetime | None = None) -> None:
    if cause not in CAUSES:
        raise ValueError("unknown intent cause")
    if cause.startswith("DECODED_DECISION") and not decision_id:
        raise ValueError("a decoded-decision cause names its decision")
    connection.execute(
        "INSERT OR IGNORE INTO cf_intent_causes (intent_id, context_id, cause, origin, decision_id, controller_profile, "
        "pipeline_fixture, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
        (intent_id, context_id, cause, origin, decision_id, controller_profile, int(pipeline_fixture),
         (now or datetime.now(timezone.utc)).isoformat()))


def causes_tx(connection: sqlite3.Connection, context_id: str) -> dict[str, dict[str, Any]]:
    """Every intent of the context with its cause (``UNLABELED`` when no row exists)."""
    result: dict[str, dict[str, Any]] = {}
    for row in connection.execute("SELECT intent_id FROM ex_intents WHERE context_id=?", (context_id,)):
        result[str(row[0])] = {"cause": "UNLABELED", "origin": None, "decision_id": None, "controller_profile": None,
                               "pipeline_fixture": False}
    for row in connection.execute(
            "SELECT intent_id, cause, origin, decision_id, controller_profile, pipeline_fixture FROM cf_intent_causes WHERE context_id=?",
            (context_id,)):
        result[str(row[0])] = {"cause": str(row[1]), "origin": str(row[2]), "decision_id": row[3], "controller_profile": row[4],
                               "pipeline_fixture": bool(row[5])}
    return result


def cause_summary_tx(connection: sqlite3.Connection, context_id: str) -> dict[str, int]:
    summary = {cause: 0 for cause in CAUSES}
    summary["UNLABELED"] = 0
    for item in causes_tx(connection, context_id).values():
        summary[item["cause"]] = summary.get(item["cause"], 0) + 1
    return summary
