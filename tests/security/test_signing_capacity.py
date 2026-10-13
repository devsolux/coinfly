from datetime import timedelta

import pytest

from coinfly.domain.context import DomainError
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
    gateway._consumed = {f"{i:032x}" for i in range(99999)}
    request = fixture.request()
    token = gateway.sign(request)
    with pytest.raises(DomainError, match="CAPACITY"):
        gateway.sign(fixture.request(request_id="c" * 32))
    assert gateway.send(request, token, lambda: 1) == 1
    assert len(gateway._consumed) == 100000
