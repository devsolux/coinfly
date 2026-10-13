"""Deduplicate representations by attested identity and declared source precedence."""

from decimal import Decimal

from coinfly.accounts.inventory import AccountKind, AccountObservation, BalanceSemantics
from coinfly.accounts.source_registry import AliasBinding, SourceRegistry
from coinfly.domain.context import DomainError, FrozenModel
from coinfly.domain.decimal_math import exact_decimal


class CanonicalPosition(FrozenModel):
    canonical_asset_id: str
    currency: str
    observation: AccountObservation
    source_observation_ids: tuple[str, ...]
    supplemental_observations: tuple[AccountObservation, ...] = ()


class CanonicalResult(FrozenModel):
    positions: tuple[CanonicalPosition, ...]
    ambiguous_accounts: tuple[str, ...]
    discrepancies: tuple[str, ...]


@exact_decimal
def canonicalize(
    registry: SourceRegistry, observations: tuple[AccountObservation, ...]
) -> CanonicalResult:
    latest: dict[tuple[str, str, str, str], AccountObservation] = {}
    identities: dict[tuple[str, str, str], AccountObservation] = {}
    discrepancies: list[str] = []
    for row in observations:
        scope = registry.scope(row.scope_id)
        if (row.authority_id != registry.authority_id or row.environment != registry.environment
                or row.source_id != scope.source_id):
            raise DomainError("OBSERVATION_REALM_MISMATCH")
        if row.portfolio_id is not None and row.portfolio_id not in scope.portfolio_ids:
            raise DomainError("OBSERVATION_PORTFOLIO_MISMATCH")
        event_key = (row.source_id, row.scope_id, row.observation_id)
        if event_key in identities and identities[event_key] != row:
            raise DomainError("OBSERVATION_ID_CONFLICT")
        identities[event_key] = row
        key = (row.source_id, row.scope_id, row.account_id, row.currency)
        prior = latest.get(key)
        if prior is not None and prior.sequence == row.sequence and prior != row:
            discrepancies.append("SOURCE_SEQUENCE_CONFLICT:" + row.scope_id + ":" + row.account_id)
        if prior is None or (row.sequence, row.as_of, row.observation_id) > (prior.sequence, prior.as_of, prior.observation_id):
            latest[key] = row

    pools: dict[str, list[tuple[AliasBinding, AccountObservation]]] = {}
    summaries: list[tuple[AliasBinding, AccountObservation]] = []
    ambiguous: list[str] = []
    for key, row in latest.items():
        binding = registry.alias(*key)
        if binding is None:
            ambiguous.append(row.scope_id + ":" + row.account_id + ":" + row.currency)
            continue
        if row.kind == AccountKind.SUMMARY:
            summaries.append((binding, row))
        else:
            pools.setdefault(binding.canonical_asset_id, []).append((binding, row))

    selected: dict[str, AccountObservation] = {}
    positions: list[CanonicalPosition] = []
    for canonical_id, aliases in sorted(pools.items()):
        # Lowest declared priority wins. Within a source priority, newest evidence wins.
        best_priority = min(binding.priority for binding, _ in aliases)
        candidates = [row for binding, row in aliases if binding.priority == best_priority]
        chosen = max(candidates, key=lambda row: (row.as_of, row.sequence, row.source_id, row.observation_id))
        for _, alternative in aliases:
            if _economic_amounts(alternative)[:2] != _economic_amounts(chosen)[:2]:
                discrepancies.append("ALIAS_VALUE_MISMATCH:" + canonical_id)
        upl_sources = [(binding, row) for binding, row in aliases
                       if row.kind == AccountKind.MARGIN or row.semantics == BalanceSemantics.EQUITY
                       or row.unsettled_perp_upl != 0]
        supplement: tuple[AccountObservation, ...] = ()
        if upl_sources:
            upl_priority = min(binding.priority for binding, _ in upl_sources)
            upl_source = max((row for binding, row in upl_sources if binding.priority == upl_priority),
                             key=lambda row: (row.as_of, row.sequence, row.source_id))
            if any(row.unsettled_perp_upl != upl_source.unsettled_perp_upl for _, row in upl_sources):
                discrepancies.append("UPL_SOURCE_MISMATCH:" + canonical_id)
            supplement = (upl_source,) if upl_source != chosen else ()
            chosen = chosen.model_copy(update={
                "quantity": chosen.settled_quantity, "semantics": BalanceSemantics.SETTLED,
                "unsettled_perp_upl": upl_source.unsettled_perp_upl,
            })
        selected[canonical_id] = chosen
        positions.append(CanonicalPosition(
            canonical_asset_id=canonical_id, currency=chosen.currency, observation=chosen,
            source_observation_ids=tuple(sorted({row.observation_id for _, row in aliases})),
            supplemental_observations=supplement,
        ))

    for binding, summary in summaries:
        component_ids = binding.summary_component_ids or (binding.canonical_asset_id,)
        components = [selected.get(key) for key in component_ids]
        if any(item is None or item.currency != summary.currency for item in components):
            discrepancies.append("SUMMARY_COMPONENTS_UNVERIFIED:" + summary.account_id)
            continue
        component_equity = sum((
            item.settled_quantity - item.liabilities + item.unsettled_perp_upl
            for item in components if item is not None
        ), Decimal(0))
        summary_equity = summary.settled_quantity - summary.liabilities + summary.unsettled_perp_upl
        if component_equity != summary_equity:
            discrepancies.append("SUMMARY_VALUE_MISMATCH:" + summary.account_id)

    return CanonicalResult(
        positions=tuple(positions), ambiguous_accounts=tuple(sorted(set(ambiguous))),
        discrepancies=tuple(sorted(set(discrepancies))),
    )


def _economic_amounts(row: AccountObservation) -> tuple[Decimal, Decimal, Decimal]:
    return row.settled_quantity, row.liabilities, row.unsettled_perp_upl
