"""Opt-in pytest plugin: PYTHONPATH=tests:. pytest -p code_only_guard ..."""
import pytest


@pytest.fixture(autouse=True)
def prohibit_live_io(monkeypatch):
    import socket
    import subprocess
    from crowdsim.infrastructure.sumo_adapter import SumoAdapter

    def forbidden(*args, **kwargs):
        raise AssertionError("Code-only checks must not start SUMO, subprocesses, listeners or network connections")

    monkeypatch.setattr(SumoAdapter, "start", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
