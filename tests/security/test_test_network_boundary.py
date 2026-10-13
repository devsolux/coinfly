import socket

import pytest


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_internet_connections_are_blocked_before_send(family):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        address = ("192.0.2.1", 443) if family == socket.AF_INET else ("2001:db8::1", 443)
        with pytest.raises(AssertionError, match="synthetic transports"):
            sock.connect(address)
        with pytest.raises(AssertionError, match="synthetic transports"):
            sock.connect_ex(address)


def test_dns_is_blocked():
    with pytest.raises(AssertionError, match="synthetic transports"):
        socket.getaddrinfo("example.invalid", 443)
