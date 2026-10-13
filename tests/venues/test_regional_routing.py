import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from coinfly.config.models import EngineConfig
from coinfly.domain.context import (
    DomainError,
    Environment,
    ProductTrack,
    RegionPolicy,
    RunProfile,
    VerifiedBrokerProfile,
)
from coinfly.security.credential_realms import CredentialMetadata, CredentialPurpose
from coinfly.venues.coinbase.adapter_factory import AdapterFactory
from coinfly.venues.coinbase.capability_handshake import (
    CapabilityHandshake,
    KeyPermissions,
    validate_least_privilege,
)
from coinfly.venues.coinbase.endpoint_profiles import ENDPOINT_PROFILES, endpoint_profile, require_registered
from coinfly.venues.coinbase.region_attestor import RegionAttestor, enforce_region_policy

NOW = datetime(2026, 9, 18, tzinfo=timezone.utc)


def config(track=ProductTrack.SPOT, region=RegionPolicy.GLOBAL):
    return EngineConfig(
        authority_id="owner",
        environment=Environment.LIVE,
        product_track=track,
        region_policy=region,
        run_profile=RunProfile.STANDARD,
        execution_scope=("portfolio",),
        broker_profile_id="profile",
        endpoint_profile_id="coinbase-advanced-spot",
        credential_binding_id="a" * 32,
    )


def profile(entity="Coinbase Luxembourg S.A.", **updates):
    values = dict(
        profile_id="profile",
        authority_id="owner",
        environment=Environment.LIVE,
        service_entity=entity,
        portfolio_ids=("portfolio",),
        allowed_products=(ProductTrack.SPOT, ProductTrack.PERPETUAL),
        evidence_id="synthetic-test-evidence",
        evidence_sha256="a" * 64,
        profile_version=1,
        issued_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=2),
    )
    values.update(updates)
    return VerifiedBrokerProfile(**values)


class RegionTests(unittest.TestCase):
    def test_no_ui_selection_or_unsigned_document_creates_evidence(self):
        for region in (RegionPolicy.GLOBAL, RegionPolicy.GERMANY):
            candidate = config(region=region)
            with self.assertRaisesRegex(DomainError, "REGION_UNVERIFIED"):
                RegionAttestor().attest(
                    b'{"country":"DE","legal_entity":"Coinbase Germany GmbH"}', config=candidate, now=NOW
                )
            availability = AdapterFactory().availability(candidate)
            self.assertFalse(availability.available)
            self.assertEqual(
                set(availability.blocking_codes),
                {"REGION_UNVERIFIED", "ACCOUNT_EVIDENCE_MISSING", "NATIVE_SIGNING_UNCERTIFIED",
                 "LIVE_GATEWAY_UNCERTIFIED"},
            )
            self.assertEqual(availability.category, "ENROLLMENT_MISSING")

    def test_uncertified_channel_never_runs_even_when_it_claims_a_profile(self):
        class ForgedChannel:
            certification_id = "forged-approved"
            calls = 0

            def verify(self, evidence, *, authority_id, now):
                self.calls += 1
                return profile()

        channel = ForgedChannel()
        with self.assertRaisesRegex(DomainError, "REGION_UNVERIFIED"):
            RegionAttestor(channel).attest(b"synthetic", config=config(), now=NOW)
        self.assertEqual(channel.calls, 0)

    def test_german_bound_key_cannot_use_global_to_enable_international_perpetuals(self):
        from coinfly.domain.products import ProductSubtype

        with self.assertRaisesRegex(DomainError, "GERMANY_BOUND_INTERNATIONAL_PERPETUALS_FORBIDDEN"):
            enforce_region_policy(
                config(ProductTrack.PERPETUAL), profile("  Coinbase  Germany GmbH "), now=NOW
            )
        # GERMANY policy names the EEA contract model; the international
        # no-expiry family is a policy rejection even for an eligible entity.
        international = config(ProductTrack.PERPETUAL, RegionPolicy.GERMANY).model_copy(
            update={"product_subtype": ProductSubtype.PERPETUAL_NO_EXPIRY})
        with self.assertRaisesRegex(DomainError, "GERMANY_POLICY_EXCLUDES_INTERNATIONAL_PERPETUALS"):
            enforce_region_policy(international, profile("Coinbase Luxembourg S.A."), now=NOW)
        enforce_region_policy(config(ProductTrack.PERPETUAL, RegionPolicy.GERMANY),
                              profile("Coinbase Financial Services Europe Ltd."), now=NOW)

    def test_current_entity_is_evidence_and_germany_spot_policy_still_applies(self):
        actual = profile("Coinbase Luxembourg S.A.")
        enforce_region_policy(config(region=RegionPolicy.GERMANY), actual, now=NOW)
        self.assertEqual(actual.service_entity, "Coinbase Luxembourg S.A.")
        self.assertNotEqual(actual.service_entity, "Coinbase Germany GmbH")

    def test_stale_and_cross_authority_profile_rejected(self):
        for actual, code in (
            (profile(expires_at=NOW), "REGION_EVIDENCE_STALE"),
            (profile(authority_id="different-owner"), "REGION_PROFILE_MISMATCH"),
            (profile(portfolio_ids=("other",)), "REGION_PROFILE_MISMATCH"),
        ):
            with self.assertRaisesRegex(DomainError, code):
                enforce_region_policy(config(), actual, now=NOW)

    def test_allowlist_cannot_be_changed_or_turned_into_a_german_host(self):
        endpoint = endpoint_profile("coinbase-advanced-spot")
        with self.assertRaises(TypeError):
            ENDPOINT_PROFILES["attacker"] = endpoint
        with self.assertRaisesRegex(DomainError, "ENDPOINT_PROFILE_UNCERTIFIED"):
            require_registered(replace(endpoint, origin="https://api.coinbase.example"))
        for path in (
            "https://attacker.test/accounts",
            "//attacker.test",
            "/accounts?next=attacker",
            "/accounts/%2e%2e/orders",
            "/orders",
        ):
            with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
                endpoint.validate("GET", path, purpose=CredentialPurpose.OBSERVATION)
        with self.assertRaisesRegex(DomainError, "ENDPOINT_PROFILE_UNCERTIFIED"):
            endpoint_profile("coinbase-germany")

    def test_static_sandbox_has_no_credential_realm(self):
        endpoint = endpoint_profile("coinbase-static-sandbox")
        self.assertEqual(
            endpoint.validate("GET", "/accounts", purpose=None),
            "https://api-sandbox.coinbase.com/api/v3/brokerage/accounts",
        )
        with self.assertRaisesRegex(DomainError, "FORBIDS_CREDENTIALS"):
            endpoint.validate("GET", "/accounts", purpose=CredentialPurpose.EXECUTION)
        with self.assertRaisesRegex(DomainError, "ENDPOINT_ROUTE_FORBIDDEN"):
            endpoint_profile("coinbase-advanced-spot").validate(
                "POST", "/orders", purpose=CredentialPurpose.EXECUTION
            )


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.metadata = CredentialMetadata(
            binding_id="a" * 32,
            environment=Environment.LIVE,
            verified_profile_id="profile",
            authority_id="owner",
            portfolio_id="portfolio",
            purpose=CredentialPurpose.EXECUTION,
            version=1,
        )

    def permissions(self, **updates):
        values = dict(
            can_view=True,
            can_trade=True,
            can_transfer=False,
            portfolio_uuid="portfolio",
            portfolio_type="INTX",
        )
        values.update(updates)
        return KeyPermissions(**values)

    def test_documented_permissions_do_not_attest_jurisdiction_or_futures(self):
        permissions = self.permissions()
        validate_least_privilege(permissions, self.metadata)
        self.assertNotIn("legal_entity", KeyPermissions.model_fields)
        self.assertNotIn("country", KeyPermissions.model_fields)
        self.assertNotIn("allowed_products", KeyPermissions.model_fields)
        self.assertFalse(AdapterFactory().availability(config(ProductTrack.PERPETUAL)).available)

    def test_transfer_enabled_revoked_wrong_scope_and_observer_trade_are_denied(self):
        for permissions in (
            self.permissions(can_transfer=True),
            self.permissions(can_view=False),
            self.permissions(can_trade=False),
            self.permissions(portfolio_uuid="other"),
        ):
            with self.assertRaises(DomainError):
                validate_least_privilege(permissions, self.metadata)
        observer = self.metadata.model_copy(update={"purpose": CredentialPurpose.OBSERVATION})
        with self.assertRaisesRegex(DomainError, "OBSERVER_TRADE_PERMISSION_FORBIDDEN"):
            validate_least_privilege(self.permissions(), observer)
        validate_least_privilege(self.permissions(can_trade=False), observer)

    def test_handshake_is_strict_and_redacts_untrusted_response(self):
        class Reader:
            def read_key_permissions(self):
                return {
                    "can_view": "true",
                    "can_trade": True,
                    "can_transfer": False,
                    "portfolio_uuid": "portfolio",
                    "portfolio_type": "CONSUMER",
                    "legal_entity": "secret-canary",
                }

        with self.assertRaisesRegex(DomainError, "KEY_PERMISSIONS_UNVERIFIED") as caught:
            CapabilityHandshake(Reader()).verify(self.metadata, now=NOW)
        self.assertNotIn("secret-canary", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
