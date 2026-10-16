"""Test-process network boundary: local IPC only, no exchange connectivity."""

import ipaddress
import socket

import pytest


def pytest_addoption(parser):
    parser.addoption("--native-ui", action="store_true", help="Open native Tk windows for desktop smoke tests")


@pytest.fixture(autouse=True)
def deny_internet_sockets(monkeypatch):
    # Synthetic transport tests inject in-process responses. AF_UNIX and loopback
    # remain available for the real worker IPC tests (and for Windows asyncio,
    # which wakes its event loop through a loopback socket pair). Neither live nor
    # sandbox HTTP is needed by this suite, so reject Internet sockets before DNS
    # or connection.
    def blocked(*_args, **_kwargs):
        raise AssertionError("Tests must use synthetic transports; Internet access is forbidden")

    def internet(sock, address):
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return False
        try:
            return not ipaddress.ip_address(address[0]).is_loopback
        except (TypeError, ValueError, IndexError):
            return True

    connect, connect_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto

    def local_connect(sock, address):
        if internet(sock, address):
            return blocked()
        return connect(sock, address)

    def local_connect_ex(sock, address):
        if internet(sock, address):
            return blocked()
        return connect_ex(sock, address)

    def local_sendto(sock, *args):
        if internet(sock, args[-1]):
            return blocked()
        return sendto(sock, *args)

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", local_sendto)
