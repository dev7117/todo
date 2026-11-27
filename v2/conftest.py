"""v2's tests run from v2/ (`cd v2 && python -m pytest`, rootdir set by v2/pytest.ini).

The repo-root v1 run (`python -m pytest -q` at the root) walks into this tree too; skip it there
so v1's run and its results stay exactly as they were.
"""
import socket
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def pytest_ignore_collect(collection_path, config):
    if config.rootpath.resolve() != HERE:
        return True
    return None


# v2 tests are always offline, including app startup in existing route tests.
# A configured test must explicitly opt in with an injected transport.
@pytest.fixture(autouse=True)
def offline_github(monkeypatch):
    for name in ("TOKEN", "REPOS", "API_BASE", "POLL_MINUTES"):
        monkeypatch.delenv(f"TODO_V2_GITHUB_{name}", raising=False)

    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError("Real network access is forbidden in v2 tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    yield
    assert not attempts, "A v2 test attempted a real network connection"
