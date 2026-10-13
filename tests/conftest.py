"""Test-process network boundary: local IPC only, no exchange connectivity."""

import socket

import pytest


def pytest_addoption(parser):
    parser.addoption("--native-ui", action="store_true", help="Open native Tk windows for desktop smoke tests")


@pytest.fixture(autouse=True)
def deny_internet_sockets(monkeypatch):
    # Synthetic transport tests inject in-process responses. AF_UNIX remains
    # available for the real worker IPC tests. Neither live nor sandbox HTTP is
    # needed by this suite, so reject Internet sockets before DNS or connection.
    def blocked(*_args, **_kwargs):
        raise AssertionError("Tests must use synthetic transports; Internet access is forbidden")

    connect, connect_ex, sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto

    def local_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return blocked()
        return connect(sock, address)

    def local_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return blocked()
        return connect_ex(sock, address)

    def local_sendto(sock, *args):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            return blocked()
        return sendto(sock, *args)

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", local_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", local_sendto)
