# Coinbase route verification note

Reviewed against the official documentation on **2026-09-21**; the Global Derivatives cutover, the EU futures note and the sequencing references were re-read on **2026-09-22** (marked inline). Every statement
below is a documentation fact with its source area; anything the documentation
did not settle is listed under *Ambiguities* and is treated as unknown by the
code. Nothing in this note is account-specific evidence; eligibility for a
real account comes only from the authenticated evidence channel.

## Sources reviewed

* Coinbase Developer Platform — Advanced Trade API reference (REST and
  WebSocket), key permissions, public endpoints, sandbox.
* Coinbase Developer Platform — Global Derivatives (Deribit-powered) API and the
  INTX migration notice.
* Coinbase newsroom / help center — EU futures launch (9 March 2026),
  perpetual-style futures descriptions, service-entity notices.

## Advanced Trade REST (spot, and US futures on the same surface)

| Fact | Value |
| --- | --- |
| Base URL | `https://api.coinbase.com/api/v3/brokerage` |
| Authentication | CDP API key; ES256 request JWT with `uri` claim (`METHOD host/path`), 120 s validity; one JWT per request |
| Numerics | JSON strings; parsed as exact `Decimal` (never floats) |
| Create order | `POST /orders` — `client_order_id`, `product_id`, `side`, `order_configuration` (`limit_limit_gtc`: `base_size`, `limit_price`, `post_only`); response `success`, `success_response.order_id`, `error_response.{error,message,new_order_failure_reason}` |
| Idempotency | `client_order_id` must be unique; a reused id is not created again and the venue returns the existing order |
| Cancel | `POST /orders/batch_cancel` — `order_ids`; per-order `success` / `failure_reason` (e.g. `UNKNOWN_CANCEL_ORDER`, `ORDER_IS_FULLY_FILLED`, `DUPLICATE_CANCEL_REQUEST`, `NOT_ALLOWED_TO_CANCEL`) |
| List orders | `GET /orders/historical/batch` — `product_ids`, `order_status`, `start_date`, `limit`, `cursor`; response `orders[]`, `has_next`, `cursor`; statuses `OPEN, FILLED, CANCELLED, EXPIRED, FAILED, UNKNOWN_ORDER_STATUS, QUEUED, CANCEL_QUEUED, PENDING` |
| Fills | `GET /orders/historical/fills` — `product_ids`, `order_ids`, `start_sequence_timestamp`, `limit`, `cursor`; `fills[]` with `entry_id`, `trade_id`, `order_id`, `price`, `size`, `commission`, `side`, `size_in_quote` |
| Accounts | `GET /accounts` with `limit`/`cursor` pagination |
| Key permissions | `GET /key_permissions` — `can_view`, `can_trade`, `can_transfer`, `portfolio_uuid`, `portfolio_type` (`UNDEFINED`, `DEFAULT`, `CONSUMER`, `INTX`) |
| Products | `product_type` `SPOT`/`FUTURE`, `product_venue` `CBE`/`FCM`/`INTX`, `future_product_details.contract_expiry_type` `PERPETUAL`/`EXPIRING`, `contract_expiry` |
| US futures | CFM venue on the same REST surface (`/cfm/*` account endpoints); "perpetual-style" futures are long-dated expiring contracts (five-year term, e.g. Dec 20 2030) with funding |
| Public data | `GET /api/v3/brokerage/market/products`, `/market/product_book`, `/market/products/{id}/candles` — unauthenticated |
| Static sandbox | `https://api-sandbox.coinbase.com/api/v3/brokerage` — no auth, predefined static responses, accounts/orders only, `X-Sandbox` header |

## Advanced Trade WebSocket (public market data)

| Fact | Value |
| --- | --- |
| URL | `wss://advanced-trade-ws.coinbase.com` (public channels); user channel on `wss://advanced-trade-ws-user.coinbase.com` |
| Subscribe | `{"type":"subscribe","channel":"level2","product_ids":[...]}`; the server disconnects a connection that has not subscribed within 5 s |
| Heartbeats | channel `heartbeats`, one message per second with `heartbeat_counter` |
| Level2 | `l2_data` events `snapshot` then `update`; `updates[].side` `bid`/`offer`, `price_level`, `new_quantity` **absolute** (0 removes the level) |
| Sequencing | **documented conflict** (re-read 2026-09-22): the level2 and heartbeats channel references say `sequence_num` is a *per-connection* message sequence "to detect dropped or out-of-order messages"; the WebSocket overview says sequence numbers are increasing integers *for each product*. `coinfly.market.sequence.SequenceTracker` judges every message against both contracts, locks the contract the stream demonstrably follows (16 consistent messages) and reports it in the feed health (`sequence.contract`); duplicates and out-of-order messages are ignored and counted, a gap under the detected contract (or under both while undetermined), a sequence reset, an out-of-order burst, or a consumer-queue overflow invalidates the books and forces a reconnect → fresh `snapshot` under a new connection generation (recorded in every observation's lineage). `tests/market/test_sequence_contract.py` |
| Freshness | transport health (state, last message, heartbeats) is reported separately from per-instrument freshness (`FeedHealth.instrument_observed_at`); the worker gates each decision on its own instrument's observation age, never on the transport alone |
| Auth | JWT per message only for authenticated channels; public channels need none |

## Global Derivatives (international perpetuals)

| Fact | Value |
| --- | --- |
| Legacy | INTX perpetual endpoints under `/api/v3/brokerage/intx/*` are deprecated |
| Cutover | hard cutover to the Deribit-powered gateway announced as "coming soon" (Global Derivatives overview, re-read 2026-09-22: "This is a hard cutover. There is no parallel-running window"); the institutional help-center FAQ (re-read 2026-09-22) lists every milestone as "September TBD, 2026" and states that all dates "are current estimates and are subject to change"; press reports named 9 September 2026 for the institutional migration, which Coinbase's own documentation does not confirm — hence `INTX_CUTOVER_STATUS_UNVERIFIED` stays; account provisioning began 2 August 2026; INTX APIs remain readable for historical data "for approximately 12 months post-migration" and "will no longer support trading after the migration date" |
| Gateway | `https://drb.coinbase.com/api/v2` (REST) and `wss://drb.coinbase.com/ws/api/v2` JSON-RPC 2.0; `public/auth` with `grant_type=coinbase_cdp` exchanging a CDP JWT for a ~15-minute HTTP token; "Prices and sizes are JSON numbers in and out" (re-read 2026-09-22) — Coinfly decodes every venue JSON number as exact `Decimal` and encodes Decimals as JSON number literals without a float (`coinfly.venues.decimal_json`); order `label` is **not** an idempotency key (the overview does not name any idempotency key); instruments like `BTC_USDC-PERPETUAL`; lowercase statuses; `private/buy|sell|cancel|edit` |
| Coinfly status | modeled as `COINBASE_GLOBAL_DERIVATIVES`, category `IMPLEMENTATION_MISSING`; no adapter exists and none is claimed |

## EEA offering (GERMANY policy)

| Fact | Value |
| --- | --- |
| Entity | derivatives via Coinbase Financial Services Europe Ltd. (CySEC 374/19); spot accounts for EEA clients via Coinbase Luxembourg S.A. (Germany historically via Coinbase Germany GmbH) |
| Launch | EU futures announced 9 March 2026 for 26 countries including Germany (Coinbase blog "Futures Contracts Now Available on Coinbase in Europe", re-read 2026-09-22: access "on the Coinbase Advanced web and mobile platform"; no API access is mentioned) |
| Contracts | perpetual-style futures (long-dated, five-year expiry, hourly funding, daily settlement) and dated monthly/quarterly futures; EUR or USDC collateral; leverage ≤ 10× |
| Onboarding | derivatives require onboarding, appropriateness assessment and KYC; a spot account with the EEA entity is a prerequisite |
| Coinfly status | subtypes `PERPETUAL_STYLE_FUTURE` / `DATED_FUTURE` under `GERMANY`; route `COINBASE_ADVANCED_FUTURES`; **API route for the EEA venue undocumented** → `CBFSE_API_ROUTE_UNDOCUMENTED` |

## Contract model used by Coinfly

| Subtype | Expiry | Funding | Documented venue |
| --- | --- | --- | --- |
| `SPOT` | none | none | CBE (Advanced Trade) |
| `PERPETUAL_NO_EXPIRY` | none | yes | International (INTX → Global Derivatives) |
| `PERPETUAL_STYLE_FUTURE` | long-dated (years) | yes | CFM (US), CBFSE (EEA) |
| `DATED_FUTURE` | monthly/quarterly | no | CFM (US), CBFSE (EEA) |

`GERMANY` never routes a perpetual-style contract as an international no-expiry
perpetual: `default_subtype(PERPETUAL, GERMANY)` is `PERPETUAL_STYLE_FUTURE`,
and a German-bound account requesting `PERPETUAL_NO_EXPIRY` is
`BROKER_RESTRICTED` (`GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN`).

## Public catalog as observed (2026-09-22, live endpoint, unauthenticated)

Records captured from `GET /market/products?product_type=SPOT|FUTURE` and the
public WebSocket (`tests/market/captures/`). They differ from the documented
shapes in ways the parser now handles explicitly:

| Observation | Consequence in Coinfly |
| --- | --- |
| 925 spot products; asset codes can be **one character** (`S`, `G`, `O`, `W`, `T`) | catalog/asset patterns accept 1–20 characters; one invalid record is excluded (`VENUE_PRODUCT_INVALID`) and never aborts the refresh |
| 118 FCM futures, all `contract_expiry_type: EXPIRING`, none `PERPETUAL` | a `PERPETUAL_NO_EXPIRY` context sees an empty catalog (reported as `other_subtypes`) |
| Futures `status` is `""` and `base_currency_id` is `""` | tradability from `is_disabled`/`view_only`/`trading_disabled` and `fcm_trading_session_details.is_session_open` (`session_closed` when closed); base asset from `future_product_details.contract_root_unit` (`BTC`, `CDEGLD`, …) |
| **Every** futures record carries `perpetual_details` (open interest); its `funding_rate` is empty even on perpetual-style contracts | the perpetual-style marker is the funding schedule on `future_product_details` (`funding_interval` "3600s", `funding_rate`, `funding_time`); monthly contracts without it are `DATED_FUTURE` |
| Futures tick in `price_increment` (5 on BTC nano, `quote_increment` stays 0.01); `base_increment`/`base_min_size` 1 contract; `contract_size` 0.01 BTC, 0.1 ETH, 5 SOL, … | paper instruments use the price tick and the contract size; marks carry the contract multiplier |
| `volume_24h` is in coins/contracts; `approximate_quote_24h_volume` in the quote currency | universe ranking uses the quote volume |
| WebSocket: one `sequence_num` per connection across `subscriptions`, `l2_data` and `heartbeats`; `l2_data` events `snapshot`/`update` with `side` `bid`/`offer`, `price_level`, `new_quantity` (0 removes) | matches the implemented per-connection contract |

Sizing consequence: one BTC nano perpetual-style contract is ≈ 865 USD of
notional (at 86.5k). With the default 100 USD paper book no futures contract
can ever be ordered (slot cap, turnover guard and the default 10 USD order
limit all sit below one contract); the universe reports
`MIN_ORDER_ABOVE_SLOT_CAP` / `MIN_ORDER_ABOVE_TURNOVER_LIMIT` /
`MIN_ORDER_ABOVE_ORDER_LIMIT` instead of owning slots that cannot trade.

## Ambiguities (kept explicit in code and status)

1. **CBFSE API route** — the EEA futures venue is documented commercially but
   no API venue/route for it is documented. Code: `CBFSE_API_ROUTE_UNDOCUMENTED`.
2. **INTX cutover date** — "coming soon"; no date. Code:
   `INTX_CUTOVER_STATUS_UNVERIFIED`. The legacy route is never selected.
3. **Perpetual-style versus dated classification** — the product schema has no
   flag. Coinfly classifies an `EXPIRING` contract as perpetual-style only when
   the record carries non-empty `perpetual_details` (the documented funding
   structure of those contracts); an `EXPIRING` contract without them is a
   dated contract when its term is within `DATED_FUTURE_MAX_TERM` (3 years) and
   is otherwise **refused as ambiguous** (`VENUE_PRODUCT_SUBTYPE_UNVERIFIED`):
   the catalog excludes it and reports the exclusion in the feed health
   (`catalog.subtype_unverified`). Term alone never selects a subtype
   (`classify_product`, re-checked 2026-09-22 against the EU launch note:
   perpetual-style futures are "long-dated futures contracts with 5-year
   expiries" using "an hourly funding mechanism"; dated contracts "have
   specific monthly or quarterly expiration dates").
4. **`new_order_failure_reason` enumeration** — only the reasons listed in
   `DEFINITIVE_NONCREATION_REASONS` are accepted as proof of non-creation; an
   unlisted reason is UNKNOWN and reconciled from history.
5. **WebSocket authentication per channel** — public level2/heartbeats need no
   JWT; the user channel does. Coinfly subscribes to public channels only.

## What this note does not do

It does not establish that any account is eligible for anything, it does not
qualify an adapter, and it does not certify a release. Those artifacts are
listed by `live.prerequisites` and in `docs/runbooks/live-arming.md`.
