"""Project-local tmp_path fixtures for Windows runs where pytest's basetemp is denied."""
from __future__ import annotations

import re
import ipaddress
import socket
import uuid
from pathlib import Path

import pytest


_socket_connect = socket.socket.connect
_socket_connect_ex = socket.socket.connect_ex
_create_connection = socket.create_connection
_getaddrinfo = socket.getaddrinfo
_gethostbyname = socket.gethostbyname


def _is_loopback(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _guard_address(address):
    host = address[0] if isinstance(address, tuple) and address else address
    if not _is_loopback(host):
        raise AssertionError(f"external network access is disabled during offline acceptance: {address!r}")


def _guarded_connect(self, address):
    _guard_address(address)
    return _socket_connect(self, address)


def _guarded_connect_ex(self, address):
    _guard_address(address)
    return _socket_connect_ex(self, address)


def _deny_external_create_connection(address, *args, **kwargs):
    _guard_address(address)
    return _create_connection(address, *args, **kwargs)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _is_loopback(host):
        raise AssertionError(f"external DNS is disabled during offline acceptance: {host!r}")
    return _getaddrinfo(host, *args, **kwargs)


def _guarded_gethostbyname(host):
    if not _is_loopback(host):
        raise AssertionError(f"external DNS is disabled during offline acceptance: {host!r}")
    return _gethostbyname(host)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.create_connection = _deny_external_create_connection
socket.getaddrinfo = _guarded_getaddrinfo
socket.gethostbyname = _guarded_gethostbyname


class WorkspaceTmpPathFactory:
    def __init__(self) -> None:
        self.root = (Path("data/eval/offline-tests") / ("pytest-workspace-tmp-r1-" + uuid.uuid4().hex)).resolve()
        self.root.mkdir(parents=True, exist_ok=False)
        self._counter = 0

    def mktemp(self, basename: str, numbered: bool = True) -> Path:
        self._counter += 1
        clean = re.sub(r"[^A-Za-z0-9_.-]", "_", basename)[:72] or "test"
        suffix = f"-{self._counter}" if numbered else ""
        target = self.root / (clean + suffix)
        target.mkdir(parents=True, exist_ok=False)
        return target

    def getbasetemp(self) -> Path:
        return self.root


@pytest.fixture(scope="session")
def tmp_path_factory() -> WorkspaceTmpPathFactory:
    return WorkspaceTmpPathFactory()


@pytest.fixture
def tmp_path(request: pytest.FixtureRequest, tmp_path_factory: WorkspaceTmpPathFactory) -> Path:
    return tmp_path_factory.mktemp(request.node.name)
