# ADR 0007 — Balance coverage and valuation

Status: accepted architecture; aggregation and read-only adapter contracts implemented; continuous Coinbase collection and account-wide completeness certification are not integrated.

Consolidated observation is presentation data for one beneficial-owner authority and one environment. It is not execution collateral or permission to transfer, withdraw, liquidate or pledge observed assets. An execution book obtains its own authoritative funds and reservations. Sandbox totals contain only synthetic sources; an explicitly separate future live observation panel must never feed paper capital.

Every enrolled source declares its authority/environment, scope, portfolio/service identity, account kinds, readability, freshness budget and discovery status. Complete coverage additionally needs authenticated evidence of the discoverable universe; a user-entered scope list is insufficient. The registry consumes worker-supplied `UniverseEvidence` and `AliasBinding` records, but no live authenticated provider for those records is integrated. Pagination completion proves completion of that query, not the existence or accessibility of every account. Partial pages, inaccessible scopes and unsupported vault/margin surfaces remain visible.

Canonical ownership uses verified account aliases and explicit source precedence. A portfolio total that summarizes a wallet is a cross-check on the same money. Two accounts with equal currency and quantity remain two assets unless identity evidence proves overlap. Ambiguous aliasing marks the result inconsistent instead of guessing. Margin-position contributions are not additional collateral pools.

For the disjoint observed asset set, settled assets at current marks equal the sum of settled quantities less recognized liabilities, each valued in the reporting currency. Explicit internal-transfer clearing adjusts asynchronous debit/credit observations without creating profit. Observed equity is that settled subtotal plus unsettled perpetual P&L once. An equity-inclusive source is normalized first. Spot unrealized P&L is a cost-basis disclosure already reflected in market value and is not added again.

| Amount | Meaning |
|---|---|
| `settled_assets_subtotal` | Valued, nonduplicated settled ownership after recognized liabilities and proven transfer clearing; it is neither cost basis nor immediately spendable cash. |
| `unsettled_perp_upl_subtotal` | Unsettled derivatives P&L not already counted in normalized settled balances. |
| `observed_equity_subtotal` | The observed, valued subset of settled assets plus unsettled derivatives P&L. Its coverage status is mandatory. |
| `complete_observed_equity` | Populated only when the snapshot meets `COMPLETE_VERIFIED`; otherwise absent. |
| `locked_execution_collateral` | Deduplicated native order/initial-margin constraints plus only incremental local reservations; not an additional asset. |
| `custody_restrictions` | Separate vault/custody restrictions. These are not interchangeable with margin requirements or a global spendability formula. |

Marks carry provenance, reporting currency and age. Stablecoins require validated prices; their name does not imply a one-to-one peg. Missing FX leaves that component unvalued and prevents a complete total. A verified empty universe can report zero; an unobserved or unvalued universe reports an absent value. Account and mark ages, the observation window, source sequences and discrepancies travel with every snapshot because asynchronous APIs do not provide one atomic exchange-wide timestamp.

| Coverage status | Interpretation |
|---|---|
| `COMPLETE_VERIFIED` | Evidence supports the declared universe, source/alias identity, fresh complete observations and required marks without a recognized equity/identity discrepancy. |
| `PARTIAL` | Known scopes, pages, access, supported surfaces or marks are missing. Any shown amount is a subtotal. |
| `STALE` | Required account or mark freshness has expired. The recorded amount is not current evidence. |
| `INCONSISTENT` | The aggregator detects a reconciliation, identity or timing conflict. Investigate the attached issues. |
| `UNKNOWN_UNIVERSE` | Available observations cannot establish whether all relevant accounts/scopes are known. |

Constraint completeness is separate from equity coverage. For example, `LOCK_OVERLAP_UNVERIFIED` leaves lock/custody/pending constraint metrics absent even when equity can be `COMPLETE_VERIFIED`. Expired universe evidence prevents completeness and can yield `UNKNOWN_UNIVERSE`; `STALE` specifically describes aged account observations or marks.

The narrow Advanced Trade account reader validates and paginates approved read-only account queries. It is a standalone integration surface: the worker currently does not enroll/poll it, and full vault/Track Accounts/perpetual sources are absent. The desktop therefore shows local simulated paper balances or `UNKNOWN_UNIVERSE` with unknown values, never a fabricated all-Coinbase total. A populated test registry demonstrates accounting behavior, not access to an actual user's accounts.

Implementation is in [source_registry.py](../../src/coinfly/accounts/source_registry.py), [collector.py](../../src/coinfly/accounts/collector.py), [canonical_positions.py](../../src/coinfly/accounts/canonical_positions.py), [balance_aggregator.py](../../src/coinfly/accounts/balance_aggregator.py), [transfer_clearing.py](../../src/coinfly/accounts/transfer_clearing.py) and [read_only_accounts.py](../../src/coinfly/venues/coinbase/read_only_accounts.py). [test_all_balance_reconciliation.py](../../tests/accounts/test_all_balance_reconciliation.py) covers overlapping summaries, equal-valued distinct accounts, UPL settlement, transfer ordering, missing FX, depegs and incomplete evidence. [test_read_only_accounts.py](../../tests/venues/test_read_only_accounts.py) covers source parsing/pagination. Follow [regional-attestation-and-balance-gaps.md](../runbooks/regional-attestation-and-balance-gaps.md) for unresolved coverage.
