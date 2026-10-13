import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from coinfly.accounts.balance_aggregator import BalanceAggregator, IncrementalReservation
from coinfly.accounts.collector import AccountCollector
from coinfly.accounts.coverage import CoverageStatus, ScopeScan
from coinfly.accounts.inventory import (
    AccountKind,
    AccountObservation,
    AccountPage,
    BalanceSemantics,
    PriceMark,
)
from coinfly.accounts.source_registry import AliasBinding, SourceRegistry, SourceScope, UniverseEvidence
from coinfly.accounts.transfer_clearing import InternalTransferEvidence
from coinfly.domain.context import DomainError, Environment
from coinfly.network.rate_governor import RateGovernor

D = Decimal
NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)


def registry(*, verified=True, scopes=("wallets",)):
    evidence = UniverseEvidence(
        authority_id="owner", environment=Environment.SANDBOX, expected_scopes=scopes,
        evidence_id="inventory-attestation", verified_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(hours=1),
    ) if verified else None
    result = SourceRegistry("owner", Environment.SANDBOX, expected_scopes=scopes, universe_evidence=evidence)
    for key in scopes:
        result.register(SourceScope(
            source_id=key, scope_id=key, authority_id="owner", environment=Environment.SANDBOX,
            portfolio_ids=("portfolio",), service_entity="Synthetic", source_api="fixture",
            readable_kinds=tuple(AccountKind), discovery_complete=True, evidence_id="fixture-source",
        ))
    return result


def observation(account="cash", *, scope="wallets", **updates):
    fields = dict(
        observation_id="obs-" + account + "-" + scope, source_id=scope, scope_id=scope,
        authority_id="owner", environment=Environment.SANDBOX, account_id=account, currency="USD",
        quantity=D("100"), as_of=NOW, sequence=1, constraints_complete=True,
    )
    fields.update(updates)
    return AccountObservation(**fields)


def alias(result, row, canonical="cash", *, priority=0, components=()):
    result.bind_alias(AliasBinding(
        source_id=row.source_id, scope_id=row.scope_id, account_id=row.account_id, currency=row.currency,
        canonical_asset_id=canonical, priority=priority, summary_component_ids=components, evidence_id="identity-evidence",
    ))


def scans(result):
    return tuple(ScopeScan(scope_id=scope.scope_id, completed=True, pages=1, observed_at=NOW, max_sequence=1)
                 for scope in result.sources)


class BalanceReconciliationTests(unittest.TestCase):
    def test_wallet_and_overlapping_portfolio_summary_are_counted_once(self):
        result = registry()
        wallet = observation()
        summary = observation("portfolio-summary", kind=AccountKind.SUMMARY)
        alias(result, wallet)
        alias(result, summary)
        snapshot = BalanceAggregator(result).aggregate((wallet, summary), scans(result), (), now=NOW)
        self.assertEqual(snapshot.complete_observed_equity, D("100"))
        self.assertEqual(snapshot.coverage.status, CoverageStatus.COMPLETE_VERIFIED)

    def test_distinct_accounts_with_identical_amounts_are_not_deduplicated(self):
        result = registry()
        first, second = observation("first"), observation("second")
        alias(result, first, "economic-first")
        alias(result, second, "economic-second")
        snapshot = BalanceAggregator(result).aggregate((first, second), scans(result), (), now=NOW)
        self.assertEqual(snapshot.complete_observed_equity, D("200"))

    def test_wallet_and_equity_endpoint_add_perpetual_upl_exactly_once(self):
        result = registry(scopes=("wallets", "margin"))
        wallet = observation(quantity=D("1000"))
        margin = observation("collateral", scope="margin", quantity=D("1050"), kind=AccountKind.MARGIN,
                             semantics=BalanceSemantics.EQUITY, unsettled_perp_upl=D("50"))
        alias(result, wallet, priority=0)
        alias(result, margin, priority=1)
        snapshot = BalanceAggregator(result).aggregate((wallet, margin), scans(result), (), now=NOW)
        self.assertEqual(snapshot.settled_assets_subtotal, D("1000"))
        self.assertEqual(snapshot.unsettled_perp_upl_subtotal, D("50"))
        self.assertEqual(snapshot.complete_observed_equity, D("1050"))

    def test_spot_upl_and_restrictions_are_nonadditive(self):
        result = registry()
        row = observation(quantity=D("1000"), spot_unrealized_pnl=D("50"), order_holds=D("100"),
                          initial_margin=D("80"), custody_restrictions=D("300"), holds_overlap_initial_margin=True)
        alias(result, row)
        reservation = IncrementalReservation(reservation_id="incremental", canonical_asset_id="cash", currency="USD", quantity=D("20"))
        snapshot = BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW,
                                                       incremental_reservations=(reservation, reservation))
        self.assertEqual(snapshot.complete_observed_equity, D("1000"))
        self.assertEqual(snapshot.spot_unrealized_pnl_disclosure, D("50"))
        self.assertEqual(snapshot.locked_execution_collateral, D("120"))
        self.assertEqual(snapshot.custody_restrictions, D("300"))
        self.assertFalse(hasattr(snapshot, "spendable_balance"))

    def test_missing_fx_is_unknown_and_stablecoin_depeg_is_valued(self):
        result = registry()
        row = observation(currency="USDC", quantity=D("1000"))
        alias(result, row)
        missing = BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW)
        self.assertIsNone(missing.observed_equity_subtotal)
        self.assertEqual(missing.coverage.missing_marks, ("USDC",))
        mark = PriceMark(currency="USDC", reporting_currency="USD", price=D("0.95"), as_of=NOW,
                         source_id="observed-fx", validated=True)
        marked = BalanceAggregator(result).aggregate((row,), scans(result), (mark,), now=NOW)
        self.assertEqual(marked.complete_observed_equity, D("950"))

    def test_unknown_alias_and_declared_universe_never_claim_complete(self):
        result = registry(verified=False)
        row = observation()
        unknown_alias = BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW)
        self.assertEqual(unknown_alias.coverage.status, CoverageStatus.INCONSISTENT)
        self.assertIsNone(unknown_alias.observed_equity_subtotal)
        alias(result, row)
        declared_only = BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW)
        self.assertEqual(declared_only.coverage.status, CoverageStatus.UNKNOWN_UNIVERSE)
        self.assertIsNone(declared_only.complete_observed_equity)
        self.assertEqual(declared_only.observed_equity_subtotal, D("100"))

    def test_source_precedence_discloses_discrepancy_and_stale_marks(self):
        result = registry(scopes=("wallets", "other"))
        first, second = observation(quantity=D("100")), observation("alias", scope="other", quantity=D("101"))
        alias(result, first, priority=0)
        alias(result, second, priority=1)
        snapshot = BalanceAggregator(result).aggregate((first, second), scans(result), (), now=NOW)
        self.assertEqual(snapshot.observed_equity_subtotal, D("100"))
        self.assertIsNone(snapshot.complete_observed_equity)
        self.assertIn("ALIAS_VALUE_MISMATCH:cash", snapshot.coverage.issues)
        stale = BalanceAggregator(result).aggregate((first, second), scans(result), (), now=NOW + timedelta(minutes=2))
        self.assertTrue(stale.coverage.stale_scopes)

    def test_tenant_and_realm_mismatch_are_rejected(self):
        result = registry()
        row = observation(environment=Environment.LIVE)
        with self.assertRaisesRegex(DomainError, "OBSERVATION_REALM_MISMATCH"):
            BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW)

    def test_ambient_decimal_precision_cannot_change_equity(self):
        result = registry()
        row = observation(quantity=D("12345678901234567890.123456789012345678"))
        alias(result, row)
        with localcontext() as arithmetic:
            arithmetic.prec = 6
            snapshot = BalanceAggregator(result).aggregate((row,), scans(result), (), now=NOW)
        self.assertEqual(snapshot.complete_observed_equity, row.quantity)

    def test_funding_settlement_preserves_equity_without_double_upl(self):
        result = registry()
        before = observation(quantity=D("1000"), kind=AccountKind.MARGIN, unsettled_perp_upl=D("50"))
        alias(result, before)
        after = observation(quantity=D("1050"), kind=AccountKind.MARGIN, unsettled_perp_upl=D("0"),
                            observation_id="settled", sequence=2)
        a = BalanceAggregator(result).aggregate((before,), scans(result), (), now=NOW)
        b = BalanceAggregator(result).aggregate((after,), scans(result), (), now=NOW)
        self.assertEqual(a.observed_equity_subtotal, b.observed_equity_subtotal)

    def test_reordered_internal_transfer_legs_preserve_wealth_with_explicit_clearing(self):
        result = registry()
        for debit_seen, credit_seen in ((False, True), (True, False), (True, True)):
            with self.subTest(debit_seen=debit_seen, credit_seen=credit_seen):
                source = observation("source", quantity=D("80") if debit_seen else D("100"))
                destination = observation("destination", quantity=D("20") if credit_seen else D("0"))
                alias(result, source, "source-asset")
                alias(result, destination, "destination-asset")
                evidence = InternalTransferEvidence(
                    transfer_id="move-1", authority_id="owner", environment=Environment.SANDBOX,
                    source_asset_id="source-asset", destination_asset_id="destination-asset", currency="USD",
                    quantity=D("20"), source_observation_id=source.observation_id,
                    destination_observation_id=destination.observation_id,
                    debit_in_source_snapshot=debit_seen, credit_in_destination_snapshot=credit_seen,
                    evidence_id="authenticated-transfer-record",
                )
                snapshot = BalanceAggregator(result).aggregate((source, destination), scans(result), (), now=NOW,
                                                                internal_transfers=(evidence, evidence))
                self.assertEqual(snapshot.complete_observed_equity, D("100"))

    def test_transfer_evidence_cannot_mint_money_into_another_realm(self):
        result = registry()
        source, destination = observation("source"), observation("destination", quantity=D("0"))
        alias(result, source, "source-asset")
        alias(result, destination, "destination-asset")
        evidence = InternalTransferEvidence(
            transfer_id="move-1", authority_id="owner", environment=Environment.LIVE,
            source_asset_id="source-asset", destination_asset_id="destination-asset", currency="USD", quantity=D("20"),
            source_observation_id=source.observation_id, destination_observation_id=destination.observation_id,
            debit_in_source_snapshot=True, credit_in_destination_snapshot=False, evidence_id="transfer-record",
        )
        with self.assertRaisesRegex(DomainError, "TRANSFER_REALM_MISMATCH"):
            BalanceAggregator(result).aggregate((source, destination), scans(result), (), now=NOW, internal_transfers=(evidence,))


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_pages_and_repeating_cursor_detection(self):
        result = registry()

        class Reader:
            def __init__(self):
                self.calls = []

            async def read_page(self, cursor):
                self.calls.append(cursor)
                if cursor is None:
                    return AccountPage(observations=(observation("one"),), next_cursor="second", complete=False)
                return AccountPage(observations=(observation("two"),))

        reader = Reader()
        result._readers["wallets"] = reader
        governor = RateGovernor()
        batch = await AccountCollector(result, governor, clock=lambda: NOW).collect()
        self.assertEqual(reader.calls, [None, "second"])
        self.assertEqual(len(batch.observations), 2)
        self.assertTrue(batch.scans[0].completed)

        class LoopReader:
            async def read_page(self, cursor):
                return AccountPage(observations=(), next_cursor="repeat", complete=False)

        result._readers["wallets"] = LoopReader()
        batch = await AccountCollector(result, governor, clock=lambda: NOW).collect()
        self.assertFalse(batch.scans[0].completed)
        self.assertEqual(batch.scans[0].issue, "PAGINATION_CURSOR_LOOP")

    async def test_second_page_failure_keeps_subtotal_and_partial_coverage(self):
        result = registry()
        row = observation()
        alias(result, row)

        class Reader:
            async def read_page(self, cursor):
                if cursor is None:
                    return AccountPage(observations=(row,), next_cursor="second", complete=False)
                raise OSError("sensitive transport payload")

        result._readers["wallets"] = Reader()
        batch = await AccountCollector(result, RateGovernor(), clock=lambda: NOW).collect()
        snapshot = BalanceAggregator(result).aggregate(batch.observations, batch.scans, (), now=NOW)
        self.assertEqual(snapshot.observed_equity_subtotal, D("100"))
        self.assertIsNone(snapshot.complete_observed_equity)
        self.assertEqual(snapshot.coverage.status, CoverageStatus.PARTIAL)
        self.assertNotIn("sensitive", snapshot.model_dump_json())
