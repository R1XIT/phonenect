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


def test_rotate_token_keeps_other_fields_and_clears_blocked_and_peers(saved):
    peers = [{"id": "x", "token": "t"}]
    cfg = {"token": "old", "port": 9000, "setup_port": 9001, "name": "PC", "files_dir": "D:\\f", "peers": peers,
           "trusted_networks": ["Home"], "blocked_peers": ["a"]}
    config.rotate_token(cfg)
    assert cfg["port"] == 9000 and cfg["setup_port"] == 9001
    assert cfg["name"] == "PC" and cfg["files_dir"] == "D:\\f"
    assert cfg["trusted_networks"] == ["Home"]
    assert cfg["blocked_peers"] == []
    assert cfg["peers"] == []
    assert saved[-1]["peers"] == []


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


def test_retry_does_not_retry_other_errors():
    calls = []

    def start():
        calls.append(1)
        raise PermissionError(13, "нет доступа")  # errno 13 без winerror: не «порт занят»

    with pytest.raises(OSError):
        retry_os_error(start, attempts=5, pause=0.01)
    assert len(calls) == 1


def test_restart_command(monkeypatch):
    import os
    import sys

    from phonenect.__main__ import restart_command

    monkeypatch.setattr(sys, "argv", ["phonenect", "--no-tray"])
    cmd, cwd = restart_command()
    assert cmd[0] == sys.executable
    assert cmd[1:3] == ["-m", "phonenect"]
    assert cmd[3:] == ["--no-tray"]
    assert os.path.isfile(os.path.join(cwd, "phonenect", "__main__.py"))


# ---------- revoke_all_devices ----------

def run_revoke(confirm=True, rotate_exc=None, spawn_exc=None, lock=None, halt=False):
    from phonenect.__main__ import revoke_all_devices

    log = []

    def rotate():
        log.append("rotate")
        if rotate_exc:
            raise rotate_exc

    def spawn():
        log.append("spawn")
        if spawn_exc:
            raise spawn_exc

    kwargs = {"lock": lock} if lock else {}
    if halt:
        kwargs["halt"] = lambda: log.append("halt")
    revoke_all_devices(lambda: confirm, rotate, spawn, lambda t: log.append(("show", t)),
                       lambda: log.append("quit"), **kwargs)
    return log


def test_revoke_ok_spawns_and_quits():
    assert run_revoke(lock=threading.Lock()) == ["rotate", "spawn", "quit"]


def test_revoke_spawn_fails_shows_message_and_quits():
    log = run_revoke(spawn_exc=OSError("x"), lock=threading.Lock())
    assert log[:2] == ["rotate", "spawn"]
    assert log[2] == ("show", "Ключ доступа заменён, но Phonenect не смог перезапуститься — запустите его вручную.")
    assert log[3] == "quit"


def test_revoke_save_fails_shows_message_and_quits_without_spawn():
    log = run_revoke(rotate_exc=OSError("диск"), lock=threading.Lock())
    assert log[0] == "rotate" and "spawn" not in log
    assert log[1] == ("show", "Не удалось сохранить новый ключ доступа: диск. Phonenect остановлен.")
    assert log[2] == "quit"


def test_revoke_declined_does_nothing_and_allows_retry():
    lock = threading.Lock()
    assert run_revoke(confirm=False, lock=lock) == []
    assert run_revoke(lock=lock) == ["rotate", "spawn", "quit"]  # замок отпущен


def test_revoke_second_click_while_dialog_open_is_ignored():
    from phonenect.__main__ import revoke_all_devices

    lock = threading.Lock()
    log = []
    inside = threading.Event()
    release = threading.Event()

    def slow_confirm():
        inside.set()
        release.wait(5)
        return True

    t = threading.Thread(target=revoke_all_devices, args=(
        slow_confirm, lambda: log.append("rotate"), lambda: log.append("spawn"), lambda t: None,
        lambda: log.append("quit"), lock))
    t.start()
    assert inside.wait(5)
    revoke_all_devices(lambda: log.append("second dialog") or True, lambda: log.append("rotate2"),
                       lambda: None, lambda t: None, lambda: None, lock)
    release.set()
    t.join(5)
    assert log == ["rotate", "spawn", "quit"]


def test_revoke_halts_before_show_and_quit_when_spawn_fails():
    log = run_revoke(spawn_exc=OSError("x"), lock=threading.Lock(), halt=True)
    assert log == ["rotate", "spawn", "halt",
                   ("show", "Ключ доступа заменён, но Phonenect не смог перезапуститься — запустите его вручную."),
                   "quit"]


def test_revoke_halts_before_show_and_quit_when_save_fails():
    log = run_revoke(rotate_exc=OSError("диск"), lock=threading.Lock(), halt=True)
    assert log == ["rotate", "halt",
                   ("show", "Не удалось сохранить новый ключ доступа: диск. Phonenect остановлен."), "quit"]
