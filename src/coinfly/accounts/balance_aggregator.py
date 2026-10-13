"""Presentation-only wealth with honest coverage, mark provenance and constraints."""

from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import Field

from coinfly.accounts.canonical_positions import canonicalize
from coinfly.accounts.coverage import CoverageReport, CoverageStatus, ScopeScan
from coinfly.accounts.inventory import AccountObservation, PriceMark
from coinfly.accounts.source_registry import SourceRegistry
from coinfly.accounts.transfer_clearing import InternalTransferEvidence
from coinfly.domain.context import DomainError, Environment, FrozenModel
from coinfly.domain.decimal_math import ExactDecimal, exact_decimal


class IncrementalReservation(FrozenModel):
    reservation_id: str
    canonical_asset_id: str
    currency: str
    quantity: ExactDecimal = Field(ge=0, allow_inf_nan=False, max_digits=38, decimal_places=18)
    already_in_venue_lock: bool = False


class BalanceSnapshot(FrozenModel):
    authority_id: str
    environment: Environment
    reporting_currency: str
    as_of: datetime
    settled_assets_subtotal: ExactDecimal | None
    unsettled_perp_upl_subtotal: ExactDecimal | None
    observed_equity_subtotal: ExactDecimal | None
    complete_observed_equity: ExactDecimal | None
    spot_unrealized_pnl_disclosure: ExactDecimal | None
    locked_execution_collateral: ExactDecimal | None
    custody_restrictions: ExactDecimal | None
    pending_transfers: ExactDecimal | None
    transfer_clearing_adjustment: ExactDecimal | None
    coverage: CoverageReport
    canonical_assets: tuple[str, ...]
    account_ages_seconds: tuple[tuple[str, ExactDecimal], ...]
    mark_ages_seconds: tuple[tuple[str, ExactDecimal], ...]
    valuation_sources: tuple[tuple[str, str], ...]

    @property
    def simulated(self) -> bool:
        return self.environment == Environment.SANDBOX


class BalanceAggregator:
    def __init__(
        self, registry: SourceRegistry, *, reporting_currency: str = "USD",
        mark_freshness_budget: timedelta = timedelta(seconds=10),
    ) -> None:
        if mark_freshness_budget <= timedelta(0):
            raise ValueError("positive mark freshness budget required")
        self.registry = registry
        self.reporting_currency = reporting_currency
        self.mark_freshness_budget = mark_freshness_budget

    @exact_decimal
    def aggregate(
        self, observations: tuple[AccountObservation, ...], scans: tuple[ScopeScan, ...],
        marks: tuple[PriceMark, ...], *, now: datetime,
        incremental_reservations: tuple[IncrementalReservation, ...] = (),
        internal_transfers: tuple[InternalTransferEvidence, ...] = (),
    ) -> BalanceSnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise DomainError("AWARE_TIMESTAMP_REQUIRED")
        normalized = canonicalize(self.registry, observations)
        scan_by_scope: dict[str, ScopeScan] = {}
        for supplied_scan in scans:
            self.registry.scope(supplied_scan.scope_id)
            if supplied_scan.scope_id in scan_by_scope:
                raise DomainError("DUPLICATE_SCOPE_SCAN")
            if supplied_scan.observed_at.tzinfo is None or supplied_scan.observed_at.utcoffset() is None:
                raise DomainError("AWARE_TIMESTAMP_REQUIRED")
            scan_by_scope[supplied_scan.scope_id] = supplied_scan
        mark_by_currency: dict[str, PriceMark] = {}
        for supplied_mark in marks:
            if supplied_mark.reporting_currency != self.reporting_currency:
                raise DomainError("MARK_REPORTING_CURRENCY_MISMATCH")
            prior = mark_by_currency.get(supplied_mark.currency)
            if prior is not None and prior.as_of == supplied_mark.as_of and prior.price != supplied_mark.price:
                raise DomainError("MARK_IDENTITY_CONFLICT")
            if prior is None or supplied_mark.as_of > prior.as_of:
                mark_by_currency[supplied_mark.currency] = supplied_mark

        issues = list(normalized.discrepancies)
        stale_scopes: set[str] = set()
        unsupported: set[str] = set()
        inaccessible: set[str] = set()
        incomplete = set(self.registry.expected_scopes) - set(scan_by_scope)
        included: set[str] = set()
        account_ages: list[tuple[str, Decimal]] = []
        for scope in self.registry.sources:
            scan = scan_by_scope.get(scope.scope_id)
            if not scope.supported:
                unsupported.add(scope.scope_id)
            if not scope.accessible or (scan is not None and scan.issue == "SOURCE_INACCESSIBLE"):
                inaccessible.add(scope.scope_id)
            if scan is None or not scan.completed or not scope.discovery_complete:
                incomplete.add(scope.scope_id)
            if scan is not None:
                if scan.completed:
                    included.add(scope.scope_id)
                if scan.issue:
                    issues.append(scan.issue + ":" + scope.scope_id)
                age = now - scan.observed_at
                if age < timedelta(0):
                    issues.append("FUTURE_SCOPE_SCAN:" + scope.scope_id)
                elif age > scope.freshness_budget:
                    stale_scopes.add(scope.scope_id)

        settled = Decimal(0)
        upl = Decimal(0)
        spot_upl = Decimal(0)
        locks = Decimal(0)
        restrictions = Decimal(0)
        transfers = Decimal(0)
        valued = 0
        constraints_known = True
        missing_marks: set[str] = set()
        stale_marks: set[str] = set()
        mark_ages: dict[str, Decimal] = {}
        valuation_sources: dict[str, str] = {}
        prices: dict[str, Decimal] = {}
        for position in normalized.positions:
            row = position.observation
            included.add(row.scope_id)
            for supplemental in position.supplemental_observations:
                supplemental_age = now - supplemental.as_of
                if supplemental_age < timedelta(0):
                    issues.append("FUTURE_ACCOUNT_OBSERVATION:" + supplemental.scope_id)
                elif supplemental_age > self.registry.scope(supplemental.scope_id).freshness_budget:
                    stale_scopes.add(supplemental.scope_id)
            age = now - row.as_of
            account_ages.append((position.canonical_asset_id, _seconds(age)))
            if age < timedelta(0):
                issues.append("FUTURE_ACCOUNT_OBSERVATION:" + row.scope_id)
                continue
            if age > self.registry.scope(row.scope_id).freshness_budget:
                stale_scopes.add(row.scope_id)
            price: Decimal | None = None
            if row.currency == self.reporting_currency:
                price = Decimal(1)
                valuation_sources[row.currency] = "REPORTING_CURRENCY_IDENTITY"
                mark_ages[row.currency] = Decimal(0)
            else:
                mark = mark_by_currency.get(row.currency)
                if mark is not None and mark.validated and mark.as_of <= now:
                    price = mark.price
                    mark_age = now - mark.as_of
                    mark_ages[row.currency] = _seconds(mark_age)
                    valuation_sources[row.currency] = mark.source_id
                    if mark_age > self.mark_freshness_budget:
                        stale_marks.add(row.currency)
                else:
                    missing_marks.add(row.currency)
            if price is None:
                constraints_known = False
                continue
            prices[position.canonical_asset_id] = price
            valued += 1
            settled += (row.settled_quantity - row.liabilities) * price
            upl += row.unsettled_perp_upl * price
            spot_upl += row.spot_unrealized_pnl * price
            transfers += row.pending_transfers * price
            if not row.constraints_complete:
                constraints_known = False
            elif row.order_holds and row.initial_margin and row.holds_overlap_initial_margin is None:
                constraints_known = False
                issues.append("LOCK_OVERLAP_UNVERIFIED:" + position.canonical_asset_id)
            else:
                native_locks = (max(row.order_holds, row.initial_margin)
                                if row.holds_overlap_initial_margin else row.order_holds + row.initial_margin)
                locks += native_locks * price
                restrictions += row.custody_restrictions * price

        reservations: dict[str, IncrementalReservation] = {}
        assets = {position.canonical_asset_id: position.currency for position in normalized.positions}
        for reservation in incremental_reservations:
            prior_reservation = reservations.get(reservation.reservation_id)
            if prior_reservation is not None:
                if prior_reservation != reservation:
                    raise DomainError("RESERVATION_ID_CONFLICT")
                continue
            reservations[reservation.reservation_id] = reservation
            if assets.get(reservation.canonical_asset_id) != reservation.currency:
                raise DomainError("RESERVATION_OUTSIDE_OBSERVED_BOOK")
            if reservation.canonical_asset_id not in prices:
                constraints_known = False
            elif not reservation.already_in_venue_lock:
                locks += reservation.quantity * prices[reservation.canonical_asset_id]

        # A source debit before the destination credit leaves a receivable in clearing;
        # a destination credit before the source debit removes the stale duplicate.
        clearing_adjustment = Decimal(0)
        cleared: dict[str, InternalTransferEvidence] = {}
        provenance = {position.canonical_asset_id: position.source_observation_ids for position in normalized.positions}
        for transfer in internal_transfers:
            prior_transfer = cleared.get(transfer.transfer_id)
            if prior_transfer is not None:
                if prior_transfer != transfer:
                    raise DomainError("TRANSFER_EVIDENCE_ID_CONFLICT")
                continue
            cleared[transfer.transfer_id] = transfer
            if transfer.authority_id != self.registry.authority_id or transfer.environment != self.registry.environment:
                raise DomainError("TRANSFER_REALM_MISMATCH")
            if (assets.get(transfer.source_asset_id) != transfer.currency
                    or assets.get(transfer.destination_asset_id) != transfer.currency
                    or transfer.source_observation_id not in provenance.get(transfer.source_asset_id, ())
                    or transfer.destination_observation_id not in provenance.get(transfer.destination_asset_id, ())):
                raise DomainError("TRANSFER_SNAPSHOT_PROVENANCE_MISMATCH")
            if transfer.source_asset_id not in prices or transfer.destination_asset_id not in prices:
                continue
            effect = int(transfer.debit_in_source_snapshot) - int(transfer.credit_in_destination_snapshot)
            clearing_adjustment += effect * transfer.quantity * prices[transfer.source_asset_id]
        settled += clearing_adjustment

        universe_verified = self.registry.universe_is_verified(now)
        inconsistent = bool(normalized.discrepancies or normalized.ambiguous_accounts
                            or any(issue.startswith(("FUTURE_", "OBSERVATION_ID_CONFLICT", "OBSERVATION_REALM_MISMATCH")) for issue in issues))
        if inconsistent:
            status = CoverageStatus.INCONSISTENT
        elif stale_scopes or stale_marks:
            status = CoverageStatus.STALE
        elif incomplete or unsupported or inaccessible or missing_marks:
            status = CoverageStatus.PARTIAL
        elif not universe_verified:
            status = CoverageStatus.UNKNOWN_UNIVERSE
        else:
            status = CoverageStatus.COMPLETE_VERIFIED
        observed_times = tuple(row.as_of for row in observations)
        coverage = CoverageReport(
            status=status, included_scopes=tuple(sorted(included)), expected_scopes=tuple(sorted(self.registry.expected_scopes)),
            unsupported_scopes=tuple(sorted(unsupported)), inaccessible_scopes=tuple(sorted(inaccessible)),
            incomplete_scopes=tuple(sorted(incomplete)), missing_marks=tuple(sorted(missing_marks)),
            ambiguous_accounts=normalized.ambiguous_accounts, stale_scopes=tuple(sorted(stale_scopes)),
            stale_marks=tuple(sorted(stale_marks)), issues=tuple(sorted(set(issues))),
            observation_start=min(observed_times) if observed_times else None,
            observation_end=max(observed_times) if observed_times else None,
            source_sequences=tuple(sorted((scan.scope_id, scan.max_sequence) for scan in scans)),
            universe_verified=universe_verified,
        )
        # A verified empty scope is zero. An unvalued or inaccessible scope is unknown.
        has_value = valued > 0 or (not observations and coverage.is_complete)
        constraints_known = constraints_known and has_value and not normalized.ambiguous_accounts
        return BalanceSnapshot(
            authority_id=self.registry.authority_id, environment=self.registry.environment,
            reporting_currency=self.reporting_currency, as_of=now,
            settled_assets_subtotal=settled if has_value else None,
            unsettled_perp_upl_subtotal=upl if has_value else None,
            observed_equity_subtotal=settled + upl if has_value else None,
            complete_observed_equity=settled + upl if coverage.is_complete and has_value else None,
            spot_unrealized_pnl_disclosure=spot_upl if has_value else None,
            locked_execution_collateral=locks if constraints_known else None,
            custody_restrictions=restrictions if constraints_known else None,
            pending_transfers=transfers if constraints_known else None,
            transfer_clearing_adjustment=clearing_adjustment if has_value else None,
            coverage=coverage, canonical_assets=tuple(sorted(assets)),
            account_ages_seconds=tuple(sorted(account_ages)), mark_ages_seconds=tuple(sorted(mark_ages.items())),
            valuation_sources=tuple(sorted(valuation_sources.items())),
        )


def _seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86400 + value.seconds) + Decimal(value.microseconds) / Decimal(1000000)
