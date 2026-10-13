# Live arming workflow and external prerequisites

Live trading is **not** available from this checkout and this document does not
change that. It describes the workflow the code enforces and names every
artifact that is still external. `live.prerequisites` (Overview capability
line / RPC) reports the same list for the current configuration, in the order
it is consumed; the first unsatisfied item is the next thing to obtain.

## Ordered prerequisites

| # | Code | Kind | Provided by |
| --- | --- | --- | --- |
| 1 | `NATIVE_SIGNING_UNCERTIFIED` | external | signed macOS ARM64 worker + vault bridge qualification (`NATIVE_BRIDGE_SHA256`) |
| 2 | `REGION_UNVERIFIED` | external | certified authenticated account-bound eligibility channel (`CERTIFIED_REGION_CHANNELS`) |
| 3 | `LIVE_GATEWAY_UNCERTIFIED` | external | active gateway certification (units, orders, protection, reconciliation) (`CERTIFIED_LIVE_GATEWAYS`) |
| 4 | `LIVE_ENDPOINT_PROFILE` | local | configuration names `coinbase-advanced-spot-execution` (present) |
| 5 | `EXECUTION_CREDENTIAL_BOUND` | operator | EXECUTION-purpose CDP key stored via Settings in the native keystore (never in chat, source, env, SQLite, IPC or exports) |
| 6 | `ACCOUNT_EVIDENCE_*` | evidence | verified service entity, jurisdiction, enrollment, fresh |
| 7 | `KEY_PERMISSIONS_*` | evidence | `GET /key_permissions`: `can_trade` true, `can_transfer` false, mandated portfolio |
| 8 | `CAPABILITY_<category>` | evidence | resolver decision `SUPPORTED` for the exact policy × product × route |
| 9 | `DATASET_READY`, `NATIVE_KERNEL_VERIFIED` | local | verified dataset and attested kernel |
| 10 | `LIVE_ADAPTER_QUALIFICATION_RUN` | external | a separately approved, small, bounded, observed real-money qualification run with recorded evidence |
| 11 | `LIVE_MANDATE_*` | operator | explicit mandate (below) |

None of these can be satisfied by a setting, an environment variable, a test
fixture or a UI action; items 1–3 are compiled trust anchors, items 6–8 come
only from the authenticated evidence channel, and item 10 is a recorded event.

## The mandate

`live.record_mandate` (owner-only RPC, `confirmation: true`) persists a
`LiveMandate` immutably with its digest: approver, the literal statement
`I authorize live orders for this authority within these limits`, environment
LIVE, product track/subtype, portfolio, allowed instruments,
`max_order_notional`, `max_gross_exposure`, `max_daily_loss` (each ≤ the engine
configuration's own limits), approval time and expiry. `live.revoke_mandate`
revokes it. `automation.enable` on a LIVE context requires a covering,
unexpired mandate (`LIVE_MANDATE_REQUIRED`) **in addition to** every other gate.

## Where the mandate sits among the other blockers

`live.prerequisites` lists every unsatisfied item together; nothing is hidden
behind an earlier item. The same list is what `automation.enable` and the
worker's engine factory refuse on: `LIVE_TRANSPORT_UNAVAILABLE: <all missing
codes>` names every missing prerequisite in one error, and
`LIVE_DERIVATIVES_ENGINE_UNAVAILABLE` says that no live engine exists for a
derivatives track at all. The mandate (item 11) is therefore never "the last
step": it is one of the blockers and is listed alongside the external ones.

## How the live spot engine is wired (once the gates are open)

The automation dispatches through one execution interface
(`coinfly.execution.engine.ExecutionEngine`); the worker verifies at bind time
that the engine implements all of it (`EXECUTION_ENGINE_INTERFACE_INCOMPLETE`),
so a route is never "wired" by duck-typing a subset. For a LIVE spot context
the worker binds `coinfly.venues.coinbase.live_spot_engine.LiveSpotEngine`,
which plans against the venue's own account evidence and dispatches through
`coinfly.venues.coinbase.advanced_trade_orders.AdvancedTradeOrderGateway` over
`BoundHttpsMutator`. The guarantees, stated precisely:

* create: **at most one initiation per intent.** The dispatcher claims the
  persisted intent, the signed transport sends one `POST /orders` with
  `client_order_id = intent id` (the venue's documented dedup key) and never
  retries. ACCEPTED only with an order id; REJECTED_NO_CREATION only for
  documented definitive reasons; any other outcome — including a failure after
  initiation — is UNKNOWN: **neither a creation nor a rejection**, fenced until
  venue evidence resolves it. This is not "exactly-once delivery"; it is one
  local attempt plus a venue-side dedup key.
* body digest vs JWT: the signing request binds the digest of the body this
  worker will send (the worker refuses to send a body it did not sign for);
  the broker verifies its own JWT claims (`uri` = method + host + path, `nbf`,
  `exp`, header `kid` + `nonce`). The JWT carries no body hash. The two are
  different evidence and are named as such.
* reconcile: complete cursor walk of `GET /orders/historical/batch` by
  `client_order_id` → positive identity (acknowledged, fence cleared); an
  absent id never becomes proof of non-creation (the only authoritative probe
  would be the idempotent re-POST, which is a mutation an operator must
  approve). Elapsed time never resolves an UNKNOWN.
* continuous sync: `sync(now)` reads `GET /accounts` (balances),
  `GET /orders/historical/batch` (order states) and
  `GET /orders/historical/fills` (fills) for every traded instrument and
  reduces them into the ledger. A documented terminal status is booked only
  once the ledger holds the fills the venue reports for that order
  (`filled_size`); the admission book is authoritative only while the last
  sync is fresh (`LIVE_ACCOUNT_SYNC_STALE` after 30 s, `LIVE_ACCOUNT_NOT_SYNCED`
  before the first).
* cancel: `POST /orders/batch_cancel`; only the venue's documented outcome is
  reported, an unknown cancel outcome leaves the intent open;
* fills: each venue fill id is booked at most once for orders this context owns
  (unknown orders are never attributed; quote-sized fills are flagged
  `FILL_UNITS_UNVERIFIED`);
* drain: open orders are cancelled through the gateway, residual spot holdings
  are closed by reduce-only limit SELLs labeled `PROTECTIVE_DRAIN`, and the
  stop certificate is issued only from the reconciled ledger.

Derivatives routes inherit none of this: there is no live derivatives engine,
no derivatives label reuses the spot gateway's idempotency claims, and the
capability resolver's decision for the exact policy × product × route is a
separate gate.

Worker-enforced limits (`ORDER_NOTIONAL_LIMIT`, `GROSS_EXPOSURE_LIMIT`,
`DRAWDOWN_LIMIT`, `DAILY_LOSS_LIMIT`, reduce-only proofs) run in the admission
actor before any dispatch; venue limits are never relied upon.

## Bounded qualification procedure (specification; never run in development)

Item 10 (`LIVE_ADAPTER_QUALIFICATION_RUN`) and item 11 (the mandate) are
circular for a first run: the trading mandate needs a recorded qualification
run, and the qualification run needs an authorization. The circle is broken by
a distinct mandate kind, not by weakening either gate:

* `LiveMandate.kind = "QUALIFICATION"` with the literal statement
  `I authorize a bounded live qualification run for this authority: one instrument, tiny notional, short expiry`,
  exactly one allowed instrument, `max_order_notional` and `max_gross_exposure`
  each ≤ 25 reporting-currency units, and a lifetime of at most 24 hours
  (`QUALIFICATION_MAX_NOTIONAL`, `QUALIFICATION_MAX_HOURS`; the model refuses
  anything wider). Such a mandate satisfies `LIVE_ADAPTER_QUALIFICATION_RUN`
  only while it is itself valid; every other prerequisite still applies.
* The run itself, executed only under a separate explicit approval that names
  its actions and limits: (1) all other prerequisites satisfied and listed
  empty by `live.prerequisites`; (2) `sync` from the real account routes with
  the operator watching Overview; (3) one small resting limit BUY at or below
  the mandate's notional, observed through `ORDER_ACKNOWLEDGED`,
  history reconciliation and fill ingestion; (4) one cancel of a resting order
  and one `PROTECTIVE_DRAIN` exit, both reconciled to a flat position;
  (5) the stop certificate and every journal row exported as the recorded
  evidence.
* Only that recorded evidence is what a later TRADING mandate cites. A
  qualification mandate permits automation only inside its own bounds: the
  admission actor enforces the engine configuration's limits, so the mandate
  covers a configuration only when that configuration is no wider than the
  mandate (`LIVE_MANDATE_QUALIFICATION_CONFIG_WIDER` otherwise) — one
  instrument, tiny notional, one day. The statement, kind and limits are part
  of the recorded mandate digest, so a qualification mandate cannot be
  presented as a trading one.

No step of this procedure was executed for this checkout, and the code
contains no way to satisfy item 10 without the recorded run.

## Explicitly not done during development

No real order was placed or cancelled, no collateral moved, no currency
converted, no account setting changed, and no live context was armed. The
static sandbox (`coinbase-static-sandbox`) remains the only remote target of
any test, and the test suite forbids Internet sockets altogether. The live
spot engine was exercised only against a documented-shape fake transport in
`tests/venues/test_live_spot_engine.py`.
