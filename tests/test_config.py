"""Отзыв доступа (новый токен) и повторные попытки занять порт при перезапуске."""
import socket
import threading
import time

import pytest

from phonenect import config
from phonenect.__main__ import retry_os_error


@pytest.fixture
def saved(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "save", lambda cfg: calls.append(dict(cfg)))
    return calls


def test_rotate_token_changes_and_saves(saved):
    cfg = {"token": "old", "port": 9000, "setup_port": 9001, "peers": [{"id": "x", "token": "t"}],
           "trusted_networks": ["Home"], "blocked_peers": ["a", "b"], "name": "PC"}
    new = config.rotate_token(cfg)
    assert new and new != "old"
    assert cfg["token"] == new
    assert len(saved) == 1 and saved[0]["token"] == new


def test_rotate_token_keeps_other_fields_and_clears_blocked(saved):
    peers = [{"id": "x", "token": "t"}]
    cfg = {"token": "old", "port": 9000, "peers": peers, "trusted_networks": ["Home"], "blocked_peers": ["a"]}
    config.rotate_token(cfg)
    assert cfg["port"] == 9000
    assert cfg["peers"] == peers
    assert cfg["trusted_networks"] == ["Home"]
    assert cfg["blocked_peers"] == []


def test_rotate_token_writes_config_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    cfg = {"token": "old", "port": 1}
    new = config.rotate_token(cfg)
    assert config.load()["token"] == new


def test_rotate_token_differs_each_time(saved):
    cfg = {"token": "old"}
    tokens = {config.rotate_token(cfg) for _ in range(5)}
    assert len(tokens) == 5


def _bind(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        s.close()
        raise
    return s


def test_retry_waits_until_port_is_free():
    holder = _bind(0)
    port = holder.getsockname()[1]
    threading.Timer(0.5, holder.close).start()
    got = retry_os_error(lambda: _bind(port), attempts=20, pause=0.1)
    try:
        assert got.getsockname()[1] == port
    finally:
        got.close()


def test_retry_gives_up_after_attempts():
    holder = _bind(0)
    port = holder.getsockname()[1]
    calls = []

    def start():
        calls.append(1)
        return _bind(port)

    try:
        t0 = time.monotonic()
        with pytest.raises(OSError):
            retry_os_error(start, attempts=3, pause=0.05)
        assert len(calls) == 3
        assert time.monotonic() - t0 < 2
    finally:
        holder.close()
