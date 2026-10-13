-- These tables share the worker transaction domain with coordinator/risk tables.
CREATE TABLE IF NOT EXISTS ex_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  authority_id TEXT NOT NULL, environment TEXT NOT NULL,
  context_id TEXT NOT NULL, scope_id TEXT NOT NULL,
  event_kind TEXT NOT NULL, entity_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_journals (
  journal_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('LIVE','SANDBOX')),
  scope_id TEXT NOT NULL, context_id TEXT NOT NULL, context_json TEXT NOT NULL,
  event_kind TEXT NOT NULL, evidence_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(authority_id, environment, scope_id, event_kind, evidence_id)
);
CREATE TABLE IF NOT EXISTS ex_entries (
  journal_id TEXT NOT NULL REFERENCES ex_journals(journal_id),
  line_no INTEGER NOT NULL, account TEXT NOT NULL, asset TEXT NOT NULL,
  amount TEXT NOT NULL, PRIMARY KEY(journal_id, line_no)
);
CREATE TABLE IF NOT EXISTS ex_intents (
  intent_id TEXT PRIMARY KEY, semantic_key TEXT NOT NULL UNIQUE,
  authority_id TEXT NOT NULL, environment TEXT NOT NULL,
  product_track TEXT NOT NULL, broker_profile_id TEXT NOT NULL,
  context_id TEXT NOT NULL, context_epoch INTEGER NOT NULL,
  scope_id TEXT NOT NULL, profile_version INTEGER NOT NULL,
  signer_version INTEGER NOT NULL, slot_id TEXT NOT NULL,
  payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ex_intents_context ON ex_intents(context_id);
CREATE TABLE IF NOT EXISTS ex_outbox (
  intent_id TEXT PRIMARY KEY REFERENCES ex_intents(intent_id),
  submission TEXT NOT NULL CHECK(submission IN ('PENDING_SUBMISSION','ACKNOWLEDGED','REJECTED')),
  dispatch TEXT NOT NULL CHECK(dispatch IN ('NOT_DISPATCHED','DISPATCH_STARTED')),
  resolution TEXT NOT NULL CHECK(resolution IN ('NONE','UNKNOWN','RESOLVED')),
  venue_order_id TEXT, filled_quantity TEXT NOT NULL,
  terminal INTEGER NOT NULL CHECK(terminal IN (0,1)), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_reservations (
  intent_id TEXT PRIMARY KEY REFERENCES ex_intents(intent_id),
  authority_id TEXT NOT NULL, environment TEXT NOT NULL, scope_id TEXT NOT NULL,
  context_id TEXT NOT NULL, asset TEXT NOT NULL,
  original_amount TEXT NOT NULL, remaining_amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_orders (
  authority_id TEXT NOT NULL, environment TEXT NOT NULL, venue_id TEXT NOT NULL,
  account_id TEXT NOT NULL, instrument TEXT NOT NULL, venue_order_id TEXT NOT NULL,
  intent_id TEXT REFERENCES ex_intents(intent_id), context_id TEXT NOT NULL,
  PRIMARY KEY(authority_id,environment,venue_id,account_id,instrument,venue_order_id)
);
CREATE TABLE IF NOT EXISTS ex_fills (
  fill_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, environment TEXT NOT NULL,
  venue_id TEXT NOT NULL, account_id TEXT NOT NULL, instrument TEXT NOT NULL,
  trade_id TEXT NOT NULL, venue_order_id TEXT NOT NULL,
  context_id TEXT NOT NULL, scope_id TEXT NOT NULL,
  intent_id TEXT REFERENCES ex_intents(intent_id), payload_json TEXT NOT NULL,
  economic_hash TEXT NOT NULL, journal_id TEXT NOT NULL REFERENCES ex_journals(journal_id),
  created_at TEXT NOT NULL,
  UNIQUE(authority_id,environment,venue_id,account_id,instrument,trade_id)
);
CREATE TABLE IF NOT EXISTS ex_positions (
  authority_id TEXT NOT NULL, environment TEXT NOT NULL, scope_id TEXT NOT NULL,
  context_id TEXT NOT NULL, instrument TEXT NOT NULL, base_asset TEXT NOT NULL,
  quote_asset TEXT NOT NULL, quantity TEXT NOT NULL, average_price TEXT NOT NULL,
  realized_pnl TEXT NOT NULL, fees_quote TEXT NOT NULL,
  PRIMARY KEY(authority_id,environment,scope_id,context_id,instrument)
);
CREATE TABLE IF NOT EXISTS ex_lot_movements (
  fill_id TEXT PRIMARY KEY REFERENCES ex_fills(fill_id),
  context_id TEXT NOT NULL, quantity_before TEXT NOT NULL, quantity_after TEXT NOT NULL,
  average_before TEXT NOT NULL, average_after TEXT NOT NULL, realized_pnl TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_fact_state (
  context_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_open_lots (
  lot_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, scope_id TEXT NOT NULL, instrument TEXT NOT NULL,
  quantity_remaining TEXT NOT NULL, price TEXT NOT NULL, multiplier TEXT NOT NULL,
  sequence INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_closed_results (
  sequence INTEGER PRIMARY KEY REFERENCES ex_events(sequence),
  authority_id TEXT NOT NULL, environment TEXT NOT NULL, context_id TEXT NOT NULL,
  scope_id TEXT NOT NULL, episode_id TEXT NOT NULL, instrument TEXT NOT NULL,
  closed_at TEXT NOT NULL, matched_pnl TEXT NOT NULL, fees TEXT NOT NULL,
  funding TEXT NOT NULL, charges TEXT NOT NULL, pending_charges INTEGER NOT NULL,
  UNIQUE(authority_id,environment,context_id,episode_id)
);
CREATE TABLE IF NOT EXISTS ex_authority_fences (
  authority_id TEXT NOT NULL, evidence_id TEXT NOT NULL, reason TEXT NOT NULL,
  context_id TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
  created_at TEXT NOT NULL, PRIMARY KEY(authority_id,evidence_id)
);
CREATE TABLE IF NOT EXISTS ex_slot_fences (
  intent_id TEXT PRIMARY KEY REFERENCES ex_intents(intent_id),
  authority_id TEXT NOT NULL, context_id TEXT NOT NULL, reason TEXT NOT NULL,
  active INTEGER NOT NULL CHECK(active IN (0,1))
);
CREATE TABLE IF NOT EXISTS ex_alarms (
  alarm_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, context_id TEXT NOT NULL,
  entity_id TEXT NOT NULL, reason TEXT NOT NULL, evidence_id TEXT NOT NULL,
  acknowledged INTEGER NOT NULL CHECK(acknowledged IN (0,1)), created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_context_retirements (
  context_id TEXT PRIMARY KEY, authority_id TEXT NOT NULL, context_json TEXT NOT NULL,
  proof_id TEXT NOT NULL, last_fact_seq INTEGER NOT NULL,
  valid INTEGER NOT NULL CHECK(valid IN (0,1)), retired_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ex_reconciliation_evidence (
  evidence_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES ex_intents(intent_id),
  kind TEXT NOT NULL, complete INTEGER NOT NULL, order_id TEXT,
  source_digest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ex_journals_no_update BEFORE UPDATE ON ex_journals BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
CREATE TRIGGER IF NOT EXISTS ex_journals_no_delete BEFORE DELETE ON ex_journals BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
CREATE TRIGGER IF NOT EXISTS ex_entries_no_update BEFORE UPDATE ON ex_entries BEGIN SELECT RAISE(ABORT,'immutable journal entry'); END;
CREATE TRIGGER IF NOT EXISTS ex_entries_no_delete BEFORE DELETE ON ex_entries BEGIN SELECT RAISE(ABORT,'immutable journal entry'); END;
CREATE TRIGGER IF NOT EXISTS ex_intents_no_update BEFORE UPDATE ON ex_intents BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
CREATE TRIGGER IF NOT EXISTS ex_intents_no_delete BEFORE DELETE ON ex_intents BEGIN SELECT RAISE(ABORT,'immutable intent'); END;
CREATE TRIGGER IF NOT EXISTS ex_fills_no_update BEFORE UPDATE ON ex_fills BEGIN SELECT RAISE(ABORT,'immutable fill'); END;
CREATE TRIGGER IF NOT EXISTS ex_fills_no_delete BEFORE DELETE ON ex_fills BEGIN SELECT RAISE(ABORT,'immutable fill'); END;
CREATE TRIGGER IF NOT EXISTS ex_events_no_update BEFORE UPDATE ON ex_events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS ex_events_no_delete BEFORE DELETE ON ex_events BEGIN SELECT RAISE(ABORT,'immutable event'); END;
CREATE TRIGGER IF NOT EXISTS ex_lots_no_update BEFORE UPDATE ON ex_lot_movements BEGIN SELECT RAISE(ABORT,'immutable lot movement'); END;
CREATE TRIGGER IF NOT EXISTS ex_lots_no_delete BEFORE DELETE ON ex_lot_movements BEGIN SELECT RAISE(ABORT,'immutable lot movement'); END;
CREATE TRIGGER IF NOT EXISTS ex_results_no_update BEFORE UPDATE ON ex_closed_results BEGIN SELECT RAISE(ABORT,'immutable closed result'); END;
CREATE TRIGGER IF NOT EXISTS ex_results_no_delete BEFORE DELETE ON ex_closed_results BEGIN SELECT RAISE(ABORT,'immutable closed result'); END;
