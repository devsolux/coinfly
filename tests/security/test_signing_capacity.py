from datetime import timedelta

import pytest

from coinfly.domain.context import DomainError
from coinfly.security import signing_gateway
from coinfly.security.signing_gateway import SignedAuthorization
from tests.security import test_context_credential_binding as fixtures

NOW = fixtures.NOW


@pytest.fixture
def fixture():
    case = fixtures.CredentialTests()
    case.setUp()
    return case


def test_expired_pending_window_is_reclaimed_without_reissuing_ids(fixture):
    gateway = fixture.gateway
    gateway._permissions = fixture.permissions.model_copy(update={"expires_at": NOW + timedelta(minutes=10)})
    requests = [fixture.request(request_id=f"{i:032x}") for i in range(256)]
    tokens = [gateway.sign(request) for request in requests]
    with pytest.raises(DomainError, match="CAPACITY"):
        gateway.sign(fixture.request())
    gateway._clock = lambda: NOW + timedelta(seconds=121)
    fresh = gateway.sign(fixture.request())
    assert len(gateway._issued) == 1
    assert len(gateway._consumed) == 256
    with pytest.raises(DomainError, match="ALREADY_ISSUED"):
        gateway.sign(requests[0])
    with pytest.raises(DomainError, match="STALE"):
        gateway.send(requests[0], tokens[0], lambda: pytest.fail("expired invocation"))
    assert gateway.send(fixture.request(), fresh, lambda: 42) == 42


def test_unissued_forgery_does_not_allocate_tombstones(fixture):
    for i in range(128):
        request = fixture.request(request_id=f"{i:032x}")
        token = SignedAuthorization(request.digest, NOW, "synthetic-forgery")
        with pytest.raises(DomainError, match="FORGERY"):
            fixture.gateway.send(request, token, lambda: pytest.fail("forged invocation"))
    assert not fixture.gateway._issued
    assert not fixture.gateway._consumed


def test_outstanding_tokens_reserve_lifecycle_capacity(fixture):
    gateway = fixture.gateway
    gateway._consumed = {f"{i:032x}": None for i in range(99999)}  # None: permanent (mutation) tombstones
    request = fixture.request()
    token = gateway.sign(request)
    with pytest.raises(DomainError, match="CAPACITY"):
        gateway.sign(fixture.request(request_id="c" * 32))
    assert gateway.send(request, token, lambda: 1) == 1
    assert len(gateway._consumed) == 100000


def _long_lived_permissions(fixture):
    fixture.gateway._permissions = fixture.permissions.model_copy(update={"expires_at": NOW + timedelta(days=1)})


def test_sustained_read_traffic_does_not_exhaust_tracking_capacity(fixture, monkeypatch):
    # Live sync signs a burst of GETs every few seconds. Spent read ids only need
    # to outlive the token window, so steady traffic must never fill the cap.
    monkeypatch.setattr(signing_gateway, "MAX_TRACKED_REQUESTS", 300)
    gateway = fixture.gateway
    _long_lived_permissions(fixture)
    for tick in range(1000):
        gateway._clock = lambda tick=tick: NOW + timedelta(seconds=tick)
        request = fixture.request(request_id=f"{tick:032x}")
        assert gateway.send(request, gateway.sign(request), lambda: tick) == tick
    assert len(gateway._issued) + len(gateway._consumed) <= signing_gateway.JWT_LIFETIME_SECONDS + 1


def test_spent_read_token_is_rejected_inside_and_after_the_pruned_window(fixture):
    gateway = fixture.gateway
    _long_lived_permissions(fixture)
    spent = fixture.request(request_id="1" * 32)
    token = gateway.sign(spent)
    assert gateway.send(spent, token, lambda: "sent") == "sent"
    with pytest.raises(DomainError, match="REPLAY_OR_FORGERY"):
        gateway.send(spent, token, lambda: pytest.fail("replayed within the token window"))
    with pytest.raises(DomainError, match="ALREADY_ISSUED"):
        gateway.sign(spent)
    gateway._clock = lambda: NOW + timedelta(seconds=signing_gateway.JWT_LIFETIME_SECONDS + 1)
    gateway.sign(fixture.request(request_id="2" * 32))  # signing prunes tombstones past the token window
    assert spent.request_id not in gateway._consumed
    with pytest.raises(DomainError, match="STALE"):
        gateway.send(spent, token, lambda: pytest.fail("replayed after the token window"))


def test_spent_mutation_ids_are_never_pruned_or_reissued(fixture):
    from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile

    gateway = fixture.gateway
    gateway._endpoint = endpoint_profile("coinbase-advanced-spot-execution")
    _long_lived_permissions(fixture)
    order = gateway.make_write_request(path="/orders", body_sha256="a" * 64)
    assert gateway.send(order, gateway.sign(order), lambda: "sent") == "sent"
    gateway._clock = lambda: NOW + timedelta(hours=6)
    gateway.sign(fixture.request(endpoint_profile_id="coinbase-advanced-spot-execution", request_id="3" * 32))
    with pytest.raises(DomainError, match="ALREADY_ISSUED"):
        gateway.sign(order)  # an order request is never silently signed twice, however much time has passed


def test_abandoned_mutation_token_id_is_never_reissued(fixture):
    from coinfly.venues.coinbase.endpoint_profiles import endpoint_profile

    gateway = fixture.gateway
    gateway._endpoint = endpoint_profile("coinbase-advanced-spot-execution")
    _long_lived_permissions(fixture)
    order = gateway.make_write_request(path="/orders", body_sha256="b" * 64)
    gateway.sign(order)  # signed but never sent: the token simply expires
    gateway._clock = lambda: NOW + timedelta(hours=6)
    gateway.sign(fixture.request(endpoint_profile_id="coinbase-advanced-spot-execution", request_id="4" * 32))
    assert gateway._consumed[order.request_id] is None
    with pytest.raises(DomainError, match="ALREADY_ISSUED"):
        gateway.sign(order)  # an expired order token is not silently reissued either
