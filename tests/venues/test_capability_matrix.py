"""All eight policy × mode × product cells against eligible and ineligible account evidence.

The resolver is the only place that decides eligibility: no configuration rule
makes Germany spot-only, and no dropdown value widens what a verified account
may do. Each cell's category and codes are pinned for four evidence fixtures.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import TestCase

from coinfly.domain.context import Environment, ProductTrack, RegionPolicy
from coinfly.domain.products import ProductSubtype, default_subtype
from coinfly.venues.capability import (
    AccountEvidence,
    CapabilityRequest,
    Category,
    LocalQualifications,
    matrix,
    resolve,
)
from coinfly.venues.coinbase.adapter_factory import default_local_qualifications
from coinfly.venues.coinbase.capability_handshake import KeyPermissions
from coinfly.venues.routes import BrokerRoute

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
PERMISSIONS = KeyPermissions(can_view=True, can_trade=True, can_transfer=False, portfolio_uuid="p1", portfolio_type="DEFAULT")


def evidence(entity: str, jurisdiction: str, subtypes: tuple[ProductSubtype, ...], *, enrolled: bool,
             permissions: KeyPermissions | None = PERMISSIONS, portfolio_type: str | None = None,
             expires: timedelta = timedelta(hours=1)) -> AccountEvidence:
    if portfolio_type and permissions is not None:
        permissions = permissions.model_copy(update={"portfolio_type": portfolio_type, "portfolio_uuid": "p2"})
    return AccountEvidence(service_entity=entity, jurisdiction=jurisdiction, allowed_subtypes=subtypes, derivatives_enrolled=enrolled,
                           permissions=permissions, portfolio_ids=("p2",) if portfolio_type else ("p1",), evidence_id=f"ev-{jurisdiction}",
                           observed_at=NOW, expires_at=NOW + expires)


GERMAN_SPOT = evidence("Coinbase Germany GmbH", "DE", (ProductSubtype.SPOT,), enrolled=False)
EEA_DERIVATIVES = evidence("Coinbase Financial Services Europe Ltd.", "DE",
                           (ProductSubtype.SPOT, ProductSubtype.PERPETUAL_STYLE_FUTURE, ProductSubtype.DATED_FUTURE), enrolled=True)
INTX = evidence("Coinbase International Exchange", "BM", (ProductSubtype.SPOT, ProductSubtype.PERPETUAL_NO_EXPIRY), enrolled=True,
                portfolio_type="INTX")

CELLS = [(env, policy, track) for env in (Environment.SANDBOX, Environment.LIVE) for policy in (RegionPolicy.GERMANY, RegionPolicy.GLOBAL)
         for track in (ProductTrack.SPOT, ProductTrack.PERPETUAL)]


def cell(env: Environment, policy: RegionPolicy, track: ProductTrack, account: AccountEvidence | None,
         local: LocalQualifications | None = None) -> tuple[Category, tuple[str, ...], BrokerRoute | None]:
    from coinfly.domain.context import RunProfile

    request = CapabilityRequest(environment=env, region_policy=policy, product_track=track,
                                run_profile=RunProfile.STANDARD if env is Environment.LIVE else RunProfile.PAPER)
    decision = resolve(request, evidence=account, now=NOW, local=local or default_local_qualifications())
    return decision.category, decision.all_blocking_codes, decision.route


class MatrixTests(TestCase):
    def test_matrix_covers_all_eight_cells_with_default_subtypes(self) -> None:
        rows = matrix(evidence=None, now=NOW, local=default_local_qualifications())
        self.assertEqual(len(rows), 8)
        seen = {(row.request.environment, row.request.region_policy, row.request.product_track, row.request.subtype) for row in rows}
        self.assertEqual(len(seen), 8)
        for row in rows:
            self.assertIs(row.request.subtype, default_subtype(row.request.product_track, row.request.region_policy))
        self.assertIs(default_subtype(ProductTrack.PERPETUAL, RegionPolicy.GERMANY), ProductSubtype.PERPETUAL_STYLE_FUTURE)
        self.assertIs(default_subtype(ProductTrack.PERPETUAL, RegionPolicy.GLOBAL), ProductSubtype.PERPETUAL_NO_EXPIRY)

    def test_every_cell_with_every_explicit_subtype_routes_and_closes_honestly(self) -> None:
        from coinfly.domain.context import RunProfile
        from coinfly.venues.routes import route_for

        subtypes = (ProductSubtype.SPOT, ProductSubtype.PERPETUAL_STYLE_FUTURE, ProductSubtype.DATED_FUTURE, ProductSubtype.PERPETUAL_NO_EXPIRY)
        accounts = {"none": None, "german_spot": GERMAN_SPOT, "eea_derivatives": EEA_DERIVATIVES, "intx": INTX}
        seen = 0
        for env, policy, track in CELLS:
            for subtype in subtypes:
                for name, account in accounts.items():
                    with self.subTest(env=env, policy=policy, track=track, subtype=subtype, account=name):
                        request = CapabilityRequest(environment=env, region_policy=policy, product_track=track, product_subtype=subtype,
                                                    run_profile=RunProfile.STANDARD if env is Environment.LIVE else RunProfile.PAPER)
                        decision = resolve(request, evidence=account, now=NOW, local=default_local_qualifications())
                        seen += 1
                        self.assertIs(decision.subtype, subtype)
                        if env is Environment.SANDBOX:
                            self.assertIs(decision.route, BrokerRoute.PAPER_LOCAL)
                        else:
                            entity = account.normalized_entity if account is not None else None
                            self.assertIs(decision.route, route_for(subtype, entity))
                            # No derivatives route is ever SUPPORTED in this build, and no
                            # derivative label inherits the spot route's qualification.
                            if subtype.is_derivative:
                                self.assertIsNot(decision.category, Category.SUPPORTED, decision.codes)
                                self.assertIsNot(decision.route, BrokerRoute.COINBASE_ADVANCED_SPOT)
                            # LIVE spot without the qualified adapter is closed too.
                            self.assertIsNot(decision.category, Category.SUPPORTED, decision.codes)
                            self.assertTrue(decision.codes, decision)
        self.assertEqual(seen, 8 * len(subtypes) * len(accounts))
        # Route facts pinned to the documented review (docs/venues/coinbase-route-verification.md).
        self.assertIs(route_for(ProductSubtype.PERPETUAL_NO_EXPIRY, None), BrokerRoute.COINBASE_GLOBAL_DERIVATIVES)
        self.assertIs(route_for(ProductSubtype.PERPETUAL_STYLE_FUTURE, None), BrokerRoute.COINBASE_ADVANCED_FUTURES)
        self.assertIs(route_for(ProductSubtype.DATED_FUTURE, None), BrokerRoute.COINBASE_ADVANCED_FUTURES)
        self.assertIs(route_for(ProductSubtype.SPOT, None), BrokerRoute.COINBASE_ADVANCED_SPOT)
        self.assertNotIn(BrokerRoute.COINBASE_INTX_LEGACY, {route_for(subtype, None) for subtype in subtypes})  # never selected

    def test_paper_cells_are_supported_locally_regardless_of_account(self) -> None:
        for account in (None, GERMAN_SPOT, EEA_DERIVATIVES, INTX):
            for policy in (RegionPolicy.GERMANY, RegionPolicy.GLOBAL):
                for track in (ProductTrack.SPOT, ProductTrack.PERPETUAL):
                    category, codes, route = cell(Environment.SANDBOX, policy, track, account)
                    self.assertIs(category, Category.SUPPORTED, (policy, track, codes))
                    self.assertIs(route, BrokerRoute.PAPER_LOCAL)
        # Without the paper derivatives qualification the derivative cells close honestly.
        category, codes, _ = cell(Environment.SANDBOX, RegionPolicy.GLOBAL, ProductTrack.PERPETUAL, None, LocalQualifications(paper_derivatives=False))
        self.assertIs(category, Category.LOCAL_QUALIFICATION_MISSING)
        self.assertIn("PERPETUAL_MARGIN_UNCERTIFIED", codes)

    def test_live_without_evidence_names_the_enrollment_gap_first(self) -> None:
        for policy in (RegionPolicy.GERMANY, RegionPolicy.GLOBAL):
            for track in (ProductTrack.SPOT, ProductTrack.PERPETUAL):
                category, codes, _ = cell(Environment.LIVE, policy, track, None)
                self.assertIs(category, Category.ENROLLMENT_MISSING)
                self.assertEqual(codes[:2], ("REGION_UNVERIFIED", "ACCOUNT_EVIDENCE_MISSING"))
                self.assertIn("LIVE_GATEWAY_UNCERTIFIED", codes)

    def test_german_spot_account(self) -> None:
        self.assertEqual(cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.SPOT, GERMAN_SPOT)[0], Category.LOCAL_QUALIFICATION_MISSING)
        category, codes, route = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.PERPETUAL, GERMAN_SPOT)
        self.assertIs(category, Category.ENROLLMENT_MISSING)
        self.assertEqual(codes[0], "DERIVATIVES_ENROLLMENT_MISSING")
        self.assertIs(route, BrokerRoute.COINBASE_ADVANCED_FUTURES)
        # A German-bound account can never be routed to international no-expiry perpetuals.
        category, codes, route = cell(Environment.LIVE, RegionPolicy.GLOBAL, ProductTrack.PERPETUAL, GERMAN_SPOT)
        self.assertIs(category, Category.BROKER_RESTRICTED)
        self.assertEqual(codes[0], "GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN")
        self.assertIs(route, BrokerRoute.COINBASE_GLOBAL_DERIVATIVES)

    def test_eea_derivatives_account_routes_perpetual_style_futures_not_intx(self) -> None:
        category, codes, route = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.PERPETUAL, EEA_DERIVATIVES)
        self.assertIs(category, Category.LOCAL_QUALIFICATION_MISSING)  # enrollment fine; only local gates remain
        self.assertIs(route, BrokerRoute.COINBASE_ADVANCED_FUTURES)
        self.assertNotIn("DERIVATIVES_ENROLLMENT_MISSING", codes)
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GLOBAL, ProductTrack.PERPETUAL, EEA_DERIVATIVES)
        self.assertIs(category, Category.BROKER_RESTRICTED)
        self.assertEqual(codes[0], "GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN")

    def test_international_account_under_germany_policy_is_policy_rejected(self) -> None:
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.SPOT, INTX)
        self.assertIs(category, Category.POLICY_REJECTED)
        self.assertEqual(codes[0], "GERMANY_POLICY_REQUIRES_EEA_ACCOUNT")
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.PERPETUAL, INTX)
        self.assertIs(category, Category.BROKER_RESTRICTED)
        self.assertEqual(codes[0], "PRODUCT_INELIGIBLE")
        category, codes, route = cell(Environment.LIVE, RegionPolicy.GLOBAL, ProductTrack.PERPETUAL, INTX)
        self.assertIs(category, Category.LOCAL_QUALIFICATION_MISSING)
        self.assertIs(route, BrokerRoute.COINBASE_GLOBAL_DERIVATIVES)
        self.assertNotIn("GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN", codes)

    def test_stale_evidence_and_missing_permissions_are_temporary_or_enrollment_gaps(self) -> None:
        stale = GERMAN_SPOT.model_copy(update={"expires_at": NOW - timedelta(seconds=1)})
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.SPOT, stale)
        self.assertIs(category, Category.TEMPORARY_FAILURE)
        self.assertIn("ACCOUNT_EVIDENCE_STALE", codes)
        no_trade = GERMAN_SPOT.model_copy(update={"permissions": PERMISSIONS.model_copy(update={"can_trade": False})})
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.SPOT, no_trade)
        self.assertIs(category, Category.ENROLLMENT_MISSING)
        self.assertIn("TRADE_PERMISSION_REQUIRED", codes)
        transfer = GERMAN_SPOT.model_copy(update={"permissions": PERMISSIONS.model_copy(update={"can_transfer": True})})
        category, codes, _ = cell(Environment.LIVE, RegionPolicy.GERMANY, ProductTrack.SPOT, transfer)
        self.assertIn("KEY_PERMISSIONS_EXCESSIVE_OR_MISSING", codes)
        self.assertIsNot(category, Category.SUPPORTED)
