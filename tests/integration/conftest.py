"""Integration-test fixtures.

The integration tests are opt-in: they require a running Katzenpost docker
mixnet and a live kpclientd reachable at 127.0.0.1:64331. Set the env var
``KATZENQT_DOCKER_INTEGRATION=1`` to enable them; otherwise all tests in
this directory are skipped at collection. Once opted in, an unreachable
kpclientd FAILS the run instead of skipping it, so a dead mixnet can never
masquerade as a green session.
"""
from __future__ import annotations

import os
import socket
import pytest


_KPCLIENTD_HOST = os.environ.get("KATZENQT_KPCLIENTD_HOST", "127.0.0.1")


def _assign_worker_port() -> int:
    """Worker gwI takes port base + I % N; base is remembered separately so a
    second import cannot re-apply the offset."""
    base = int(os.environ.setdefault(
        "KATZENQT_KPCLIENTD_BASE_PORT",
        os.environ.get("KATZENQT_KPCLIENTD_PORT", "64331"),
    ))
    count = int(os.environ.get("KATZENQT_KPCLIENTD_COUNT", "1"))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "")
    if count <= 1 or not worker.startswith("gw"):
        return base
    port = base + int(worker[2:]) % count
    os.environ["KATZENQT_KPCLIENTD_PORT"] = str(port)
    return port


_KPCLIENTD_PORT = _assign_worker_port()


def _kpclientd_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pytest_collection_modifyitems(config, items):
    if os.environ.get("KATZENQT_DOCKER_INTEGRATION") == "1":
        return
    skip_marker = pytest.mark.skip(
        reason="set KATZENQT_DOCKER_INTEGRATION=1 to run docker integration tests"
    )
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip_marker)


@pytest.fixture(scope="session")
def kpclientd_endpoint():
    """Assert the docker mixnet's kpclientd is reachable before running."""
    if not _kpclientd_reachable(_KPCLIENTD_HOST, _KPCLIENTD_PORT):
        pytest.fail(
            f"KATZENQT_DOCKER_INTEGRATION=1 is set but kpclientd is not "
            f"reachable at {_KPCLIENTD_HOST}:{_KPCLIENTD_PORT}; start the "
            f"docker mixnet first (katzenpost/docker: make start wait)"
        )
    return (_KPCLIENTD_HOST, _KPCLIENTD_PORT)
